"""Transition & Structural Forecasting Model (TSF).

Models how the *structural shape* of a draw (sum regime, odd/even split,
decade concentration, consecutive pairs, persistence, recurrence from the
previous draw) tends to transition from one draw to the next, rather than
asking which individual numbers are "due." Builds three competing
hypotheses (Primary/Persistence/Variance) about the next draw's structure,
generates one prediction line per hypothesis, and scores each dimension
independently after the real result is known so the model's assumptions
can be judged over time instead of only counting exact-number hits.

Deliberately has zero numerology/astrology/moon-phase inputs -- kept
separate from cosmic_engine.py so that layer can eventually be tested for
whether it adds anything, rather than contaminating this analysis.

Automatic self-reweighting (keep/downweight/remove/add assumptions based
on scored history) is intentionally NOT implemented here -- there isn't
enough scored out-of-sample history yet to calibrate that against, and
doing so on too little evidence is exactly the mistake this model exists
to avoid. track_record_summary() surfaces per-dimension hit rates so a
human can judge model health; MODEL_VERSION exists so a future
auto-tuning generation won't silently blend into this one's track record.
"""

import random
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from pattern_engine import load_draws, frequency_analysis, gap_analysis
from match_engine import compute_features, _decade_bins

# v2 has no shared game_config.py (unlike v1) -- table/bonus config is
# duplicated inline across api.py/cosmic_engine.py/pattern_engine.py/
# match_engine.py there too, so this mirrors that existing pattern rather
# than introducing a refactor v2 doesn't otherwise have. draw_weekdays use
# date.weekday() convention (Mon=0..Sun=6), verified against the live DB:
# powerball draws Mon/Wed/Sat, megamillions draws Tue/Fri.
TSF_GAME_CONFIG = {
    "powerball": {
        "table": "powerball_draws", "main_count": 5, "max_num": 69,
        "doubleplay_game_value": "powerball_doubleplay", "draw_weekdays": [0, 2, 5],
    },
    "megamillions": {
        "table": "megamillions_draws", "main_count": 5, "max_num": 70,
        "doubleplay_game_value": None, "draw_weekdays": [1, 4],
    },
}


def get_config(game):
    return TSF_GAME_CONFIG.get(game, TSF_GAME_CONFIG["powerball"])

# ============================================================
# CALIBRATION CONSTANTS -- small and visible on purpose, tune here
# ============================================================

STRUCTURAL_WINDOW_SHORT = 10
STRUCTURAL_WINDOW_LONG = 20
CONCENTRATION_WINDOW_FRACTION = 0.36   # generalizes 1-19/10-29/... to any max_num
CONCENTRATION_THRESHOLD = 2 / 3        # >=2/3 of the draw's numbers in one window => concentrated (main-count-agnostic)
PERSISTENCE_HIGH = 0.8                 # >=80% of window draws touched this decade bin
PERSISTENCE_LOW = 0.4                  # <40% => rare; between => intermittent
MIN_TRANSITION_SAMPLE = 30             # below this, confidence is forced Low
LIFT_HIGH = 1.15                       # observed/baseline ratio for High confidence
LIFT_LOW = 0.9                         # below this, less likely than baseline => Low
HIGH_SAMPLE_FOR_HIGH_CONF = 100
NEIGHBOR_K = 15                        # how many structurally similar past draws to consider
NEIGHBOR_MIN_SAMPLE_FOR_SIGNAL = 8      # below this many followups on record, ignore the signal
MODEL_VERSION = "tsf-v1"

DIMENSIONS = ['sum_regime', 'parity_regime', 'low_high_regime', 'concentration_regime', 'consecutive_regime']
CORE_DIMS = ['sum_regime', 'low_high_regime', 'concentration_regime']
DIM_LABELS = {
    'sum_regime': 'Sum',
    'parity_regime': 'Odd/even',
    'low_high_regime': 'Low/high',
    'concentration_regime': 'Concentration',
    'consecutive_regime': 'Consecutive pairs',
}
CONF_RANK = {'Low': 0, 'Medium': 1, 'High': 2}
REVERSE_MAP = {
    'sum_regime': {'low': 'high', 'high': 'low'},
    'parity_regime': {'odd_heavy': 'even_heavy', 'even_heavy': 'odd_heavy'},
    'low_high_regime': {'low_heavy': 'high_heavy', 'high_heavy': 'low_heavy'},
    'concentration_regime': {'concentrated': 'dispersed', 'dispersed': 'concentrated'},
    'consecutive_regime': {'none': 'multiple', 'multiple': 'none'},
}

# ============================================================
# 1. PER-DRAW STRUCTURAL STATE (step 1)
# ============================================================

def _decade_windows(max_num):
    """Overlapping sliding bands (~36% of the number range wide) used only
    for concentration scoring -- distinct from _decade_bins()'s
    non-overlapping 10-wide bins used for the histogram. Generalizes the
    literal 1-19/10-29/20-39/30-49 Florida-Lotto-specific bands from the
    spec to any max_num (e.g. Mega Millions' 70) without a hardcoded
    constant."""
    width = max(1, round(max_num * CONCENTRATION_WINDOW_FRACTION))
    step = max(1, width // 3)
    windows = []
    lo = 1
    while lo <= max_num:
        hi = min(lo + width - 1, max_num)
        windows.append((lo, hi))
        if hi == max_num:
            break
        lo += step
    return windows


def concentration_score(numbers, max_num):
    best_count, best_window = 0, None
    for lo, hi in _decade_windows(max_num):
        count = sum(1 for n in numbers if lo <= n <= hi)
        if count > best_count:
            best_count, best_window = count, (lo, hi)
    main_count = len(numbers) or 1
    score = best_count / main_count
    regime = 'concentrated' if score >= CONCENTRATION_THRESHOLD else 'dispersed'
    return {
        'score': round(score, 3),
        'regime': regime,
        'best_window': list(best_window) if best_window else None,
        'best_count': best_count,
    }


def structural_state(numbers, max_num):
    """Step 1's per-draw vector: match_engine.compute_features() (sum,
    odd/even, low/high, spread, consecutive pairs, decade histogram) plus
    concentration scoring."""
    feats = compute_features(numbers, max_num)
    conc = concentration_score(numbers, max_num)
    feats['concentration_score'] = conc['score']
    feats['concentration_regime'] = conc['regime']
    feats['best_window'] = conc['best_window']
    return feats


def previous_draw_deltas(numbers, prev_numbers):
    """Repeated numbers and repeated last-digits vs. the chronologically
    previous draw. Recurrence is treated as a measurable feature here, not
    grounds to exclude a number -- see build_candidate_pool()."""
    if prev_numbers is None:
        return None
    cur_set, prev_set = set(numbers), set(prev_numbers)
    repeat_numbers = sorted(cur_set & prev_set)
    cur_digits = {n % 10 for n in numbers}
    prev_digits = {n % 10 for n in prev_numbers}
    repeated_digits = sorted(cur_digits & prev_digits)
    return {
        'repeat_count': len(repeat_numbers),
        'repeat_numbers': repeat_numbers,
        'repeated_last_digit_count': len(repeated_digits),
        'repeated_last_digits': repeated_digits,
    }


def sum_regime_cutoffs(draws):
    """33rd/67th percentile of this game's full historical sum
    distribution -- quantile-based rather than a fixed number, so low/
    medium/high works identically whether max_num is 69 or 70."""
    sums = sorted(sum(d['numbers']) for d in draws)
    n = len(sums)
    if n == 0:
        return (0, 0)

    def pct(p):
        idx = min(n - 1, max(0, int(round(p * (n - 1)))))
        return sums[idx]

    return (pct(1 / 3), pct(2 / 3))


def classify_regimes(state, cutoffs, main_count):
    low_hi, med_hi = cutoffs
    s = state['sum']
    sum_regime = 'low' if s <= low_hi else ('medium' if s <= med_hi else 'high')

    # Tolerance-based rather than an exact-equality check against
    # main_count/2 -- for an odd main_count (v2's 5-number games), half is
    # a fractional 2.5, which an integer odd_count can never equal, so an
    # exact-equality "balanced" would be unreachable. Within 0.5 of half
    # captures the closest-to-even split possible (e.g. 2-3 or 3-2 of 5)
    # as balanced, while still reproducing the original exact-equality
    # behavior for even main_count (e.g. 3-3 of 6).
    half = main_count / 2
    if state['odd_count'] - half > 0.5:
        parity_regime = 'odd_heavy'
    elif half - state['odd_count'] > 0.5:
        parity_regime = 'even_heavy'
    else:
        parity_regime = 'balanced'

    if state['low_count'] - half > 0.5:
        low_high_regime = 'low_heavy'
    elif half - state['low_count'] > 0.5:
        low_high_regime = 'high_heavy'
    else:
        low_high_regime = 'balanced'

    cp = state['consecutive_pairs']
    consecutive_regime = 'none' if cp == 0 else ('one' if cp == 1 else 'multiple')

    return {
        'sum_regime': sum_regime,
        'parity_regime': parity_regime,
        'low_high_regime': low_high_regime,
        'concentration_regime': state['concentration_regime'],
        'consecutive_regime': consecutive_regime,
    }


def build_structural_series(draws, max_num, main_count):
    """The backbone: one entry per draw (most-recent-first, index-aligned
    with `draws`), combining structural_state + previous_draw_deltas +
    classify_regimes. Every function below consumes this instead of
    recomputing from raw draws."""
    cutoffs = sum_regime_cutoffs(draws)
    series = []
    n = len(draws)
    for i, d in enumerate(draws):
        state = structural_state(d['numbers'], max_num)
        prev_numbers = draws[i + 1]['numbers'] if i + 1 < n else None
        prev_deltas = previous_draw_deltas(d['numbers'], prev_numbers)
        regimes = classify_regimes(state, cutoffs, main_count)
        series.append({
            'date': d['date'],
            'numbers': d['numbers'],
            'state': state,
            'prev_deltas': prev_deltas,
            'regimes': regimes,
        })
    return series, cutoffs


def current_structural_state(series, n=STRUCTURAL_WINDOW_SHORT):
    latest = series[0]
    recent = series[:n]
    avg_sum = sum(s['state']['sum'] for s in recent) / len(recent)
    avg_odd = sum(s['state']['odd_count'] for s in recent) / len(recent)
    return {
        'date': latest['date'],
        'numbers': latest['numbers'],
        'state': latest['state'],
        'prev_deltas': latest['prev_deltas'],
        'regimes': latest['regimes'],
        'recent_window': {
            'n': len(recent),
            'avg_sum': round(avg_sum, 1),
            'avg_odd_count': round(avg_odd, 2),
        },
    }

# ============================================================
# 2. PERSISTENT ZONES (step 2) -- separate from "overdue"
# ============================================================

def persistent_zones(series, decade_bins, windows=(STRUCTURAL_WINDOW_SHORT, STRUCTURAL_WINDOW_LONG)):
    result = {}
    for bidx, (lo, hi) in enumerate(decade_bins):
        entry = {'range': [lo, hi]}
        for w in windows:
            subset = series[:w]
            coverage = (sum(1 for s in subset if any(lo <= n <= hi for n in s['numbers'])) / len(subset)) if subset else 0.0
            if coverage >= PERSISTENCE_HIGH:
                label = 'persistent'
            elif coverage < PERSISTENCE_LOW:
                label = 'rare'
            else:
                label = 'intermittent'
            entry[f'window_{w}'] = {'coverage': round(coverage, 3), 'label': label, 'sample_size': len(subset)}
        result[bidx] = entry
    return result

# ============================================================
# 3. STRUCTURAL TRANSITIONS (step 3) -- the core new piece.
# Nothing else in this codebase walks the draw sequence pairwise and
# tallies transition frequencies between structural categories;
# pattern_engine.markov_analysis() is a NUMBER-level Markov chain, not a
# draw-level structural one.
# ============================================================

def _count_bucket(count):
    return 'none' if count == 0 else ('few' if count == 1 else 'many')


def structural_transitions(series):
    tables = {d: defaultdict(Counter) for d in DIMENSIONS}
    decade_table = defaultdict(lambda: defaultdict(Counter))

    n = len(series)
    for i in range(n - 1):
        later, earlier = series[i], series[i + 1]  # earlier is chronologically first
        for d in DIMENSIONS:
            tables[d][earlier['regimes'][d]][later['regimes'][d]] += 1
        for bidx, (prev_count, next_count) in enumerate(zip(earlier['state']['decade_histogram'], later['state']['decade_histogram'])):
            decade_table[bidx][_count_bucket(prev_count)][_count_bucket(next_count)] += 1

    dim_result = {}
    for d in DIMENSIONS:
        dim_result[d] = {}
        for frm, to_counter in tables[d].items():
            total = sum(to_counter.values())
            dim_result[d][frm] = {
                'total': total,
                'to': {to: {'count': c, 'pct': round(c / total, 4)} for to, c in to_counter.items()},
            }

    decade_result = {}
    for bidx, prev_map in decade_table.items():
        decade_result[bidx] = {}
        for prev_bucket, next_counter in prev_map.items():
            total = sum(next_counter.values())
            decade_result[bidx][prev_bucket] = {
                'total': total,
                'to': {nb: {'count': c, 'pct': round(c / total, 4)} for nb, c in next_counter.items()},
            }

    return {'dimensions': dim_result, 'decade_bins': decade_result}


def _baseline_rate(series, dimension, value):
    counts = Counter(s['regimes'][dimension] for s in series)
    total = sum(counts.values())
    return counts.get(value, 0) / total if total else 0


def confidence_label(from_count, observed_count, baseline_pct):
    if from_count < MIN_TRANSITION_SAMPLE:
        return 'Low'
    observed_pct = observed_count / from_count if from_count else 0
    if baseline_pct <= 0:
        lift = float('inf') if observed_pct > 0 else 0
    else:
        lift = observed_pct / baseline_pct
    if lift < LIFT_LOW:
        return 'Low'
    if lift >= LIFT_HIGH and from_count >= HIGH_SAMPLE_FOR_HIGH_CONF:
        return 'High'
    return 'Medium'


def transition_lookup(transitions_result, current_regimes, series):
    """Turns 'occurred in 42% of comparable historical transitions' into a
    defensible label by comparing against the unconditional base rate --
    since the lottery is random, most conditional probabilities should sit
    near baseline, so only a real, well-sampled deviation earns High."""
    dims_result = transitions_result['dimensions']
    lookup = {}
    for dim, frm_val in current_regimes.items():
        frm_row = dims_result.get(dim, {}).get(frm_val)
        entry = {'from': frm_val, 'sample_size': 0, 'to_values': {}}
        if frm_row:
            entry['sample_size'] = frm_row['total']
            for to_val, stats in frm_row['to'].items():
                baseline = _baseline_rate(series, dim, to_val)
                label = confidence_label(frm_row['total'], stats['count'], baseline)
                entry['to_values'][to_val] = {
                    'count': stats['count'],
                    'pct': stats['pct'],
                    'baseline_pct': round(baseline, 4),
                    'lift': round(stats['pct'] / baseline, 3) if baseline > 0 else None,
                    'confidence': label,
                }
        lookup[dim] = entry
    return lookup

# ============================================================
# 3b. NEAREST-NEIGHBOR STRUCTURAL SIGNAL
# Complements structural_transitions()'s whole-history aggregate (every
# draw sharing just the current from-value on one dimension) with a
# conditioned-on-shape signal: of the K historical draws most similar to
# the CURRENT draw overall (via match_engine's similarity search, the same
# engine behind the Pattern Match tab), what did the draw that followed
# each of them actually look like structurally? Smaller, noisier sample
# than the aggregate table, so it's blended in as corroboration/dissent
# rather than a replacement -- see build_hypotheses().
# ============================================================

def neighbor_structural_signal(game, max_num, main_count, cutoffs, k=NEIGHBOR_K):
    """Only supported for the main stream -- match_engine's similarity
    search has no equivalent Double Play stream isolation, so this
    returns None for draw_type='doubleplay' rather than mixing streams."""
    from match_engine import pattern_match

    result = pattern_match(game, limit=k)
    if not result:
        return None

    regime_tally = {d: Counter() for d in DIMENSIONS}
    number_counts = Counter()
    sample_size = 0
    match_summaries = []
    for m in result['matches']:
        nd = m.get('next_drawing')
        if not nd:
            continue
        sample_size += 1
        followup_state = structural_state(nd['numbers'], max_num)
        followup_regimes = classify_regimes(followup_state, cutoffs, main_count)
        for d in DIMENSIONS:
            regime_tally[d][followup_regimes[d]] += 1
        for n in nd['numbers']:
            number_counts[n] += 1
        match_summaries.append({
            'date': m['date'],
            'numbers': m['numbers'],
            'similarity': m['score'],
            'next_drawing': {'date': nd['date'], 'numbers': nd['numbers']},
        })

    by_dimension = {}
    for d in DIMENSIONS:
        total = sum(regime_tally[d].values())
        if total:
            by_dimension[d] = {val: {'count': c, 'pct': round(c / total, 4)} for val, c in regime_tally[d].items()}

    return {
        'sample_size': sample_size,
        'k_requested': k,
        'matches': match_summaries,
        'by_dimension': by_dimension,
        'top_numbers': [{'number': n, 'count': c} for n, c in number_counts.most_common(15)],
    }


def _neighbor_top(neighbor_signal, dim):
    if not neighbor_signal or neighbor_signal['sample_size'] < NEIGHBOR_MIN_SAMPLE_FOR_SIGNAL:
        return None, None
    dist = neighbor_signal['by_dimension'].get(dim)
    if not dist:
        return None, None
    top_val = max(dist, key=lambda v: dist[v]['pct'])
    return top_val, dist[top_val]['pct']

# ============================================================
# 4. THREE COMPETING HYPOTHESES (step 4)
# ============================================================

def _primary_target(entry, frm_val):
    if not entry['to_values']:
        return frm_val
    return max(entry['to_values'], key=lambda v: entry['to_values'][v]['pct'])


def _variance_target(dim, frm_val, entry):
    direct = REVERSE_MAP.get(dim, {}).get(frm_val)
    to_values = entry['to_values']
    if direct and direct in to_values and to_values[direct]['count'] > 0:
        return direct
    candidates = {v: s['count'] for v, s in to_values.items() if v != frm_val}
    if candidates:
        return max(candidates, key=candidates.get)
    return direct or frm_val


def _weakest_link(confs):
    vals = [confs[d] for d in CORE_DIMS if d in confs]
    return min(vals, key=lambda c: CONF_RANK[c]) if vals else 'Low'


NEIGHBOR_AGREEMENT_BONUS = {'Low': 'Medium', 'Medium': 'High', 'High': 'High'}


def build_hypotheses(current_regimes, lookup, cfg, neighbor_signal=None):
    hypotheses = []

    def _line(label, name, target_fn):
        targets, confs, parts = {}, {}, []
        for d in DIMENSIONS:
            entry = lookup.get(d, {'to_values': {}, 'sample_size': 0})
            frm_val = current_regimes[d]
            tgt = target_fn(d, frm_val, entry)
            stat = entry['to_values'].get(tgt)
            conf = stat['confidence'] if stat else 'Low'

            # Corroborate/dissent against the K most structurally similar
            # historical draws, when there's enough of them with a
            # followup on record to say anything -- only meaningfully
            # applied to Primary (Persistence's target is always "stay put"
            # by construction; Variance's target_fn already consults the
            # neighbor signal itself, see _variance_target_with_neighbor).
            neighbor_note = ''
            if label == 'Primary':
                neighbor_val, neighbor_pct = _neighbor_top(neighbor_signal, d)
                if neighbor_val is not None:
                    if neighbor_val == tgt:
                        conf = NEIGHBOR_AGREEMENT_BONUS.get(conf, conf)
                        neighbor_note = (
                            f" The {neighbor_signal['sample_size']} most structurally similar historical "
                            f"draws agree ({neighbor_pct*100:.0f}% of their followups landed here too)."
                        )
                    else:
                        neighbor_note = (
                            f" Note: the {neighbor_signal['sample_size']} most structurally similar historical "
                            f"draws leaned toward \"{neighbor_val}\" instead ({neighbor_pct*100:.0f}%) -- kept "
                            f"the larger-sample whole-history value here since it's more stable."
                        )

            targets[d], confs[d] = tgt, conf
            if stat:
                parts.append(
                    f"{DIM_LABELS[d]}: {frm_val}→{tgt} occurred in {stat['pct']*100:.0f}% of "
                    f"{entry['sample_size']} comparable draws (baseline {stat['baseline_pct']*100:.0f}%, "
                    f"lift {stat['lift']}) — {conf} confidence.{neighbor_note}"
                )
            else:
                parts.append(f"{DIM_LABELS[d]}: {frm_val}→{tgt} has no precedent in this exact state — Low confidence.{neighbor_note}")
        return {
            'label': label,
            'name': name,
            'target_regimes': targets,
            'confidence': _weakest_link(confs),
            'per_dimension_confidence': confs,
            'rationale': ' '.join(parts),
        }

    def _variance_target_with_neighbor(d, frm, e):
        # Prefer the neighbor-favored alternative as a more evidence-
        # grounded reversal candidate than the generic opposite-flip, but
        # only when it's actually been observed historically from this
        # exact from-state (never propose an unprecedented transition).
        neighbor_val, _ = _neighbor_top(neighbor_signal, d)
        if neighbor_val is not None and neighbor_val != frm:
            if neighbor_val in e['to_values'] and e['to_values'][neighbor_val]['count'] > 0:
                return neighbor_val
        return _variance_target(d, frm, e)

    hypotheses.append(_line('Primary', 'Most likely transition', lambda d, frm, e: _primary_target(e, frm)))
    hypotheses.append(_line('Persistence', 'Persistence / continuation scenario', lambda d, frm, e: frm))
    hypotheses.append(_line('Variance', 'Variance / reversal scenario', _variance_target_with_neighbor))

    return hypotheses

# ============================================================
# 5/7/8. FREQUENCY, RECURRENCE, CONCENTRATION PROFILES
# (steps 5 and 8's explicit, non-"due" base rates)
# ============================================================

def recurrence_profile(series):
    with_prev = [s for s in series if s['prev_deltas'] is not None]
    if not with_prev:
        return {'avg_repeat_count': 0, 'repeat_count_histogram': {}, 'avg_repeated_last_digit_count': 0, 'sample_size': 0}
    repeat_counts = [s['prev_deltas']['repeat_count'] for s in with_prev]
    digit_counts = [s['prev_deltas']['repeated_last_digit_count'] for s in with_prev]
    return {
        'avg_repeat_count': round(sum(repeat_counts) / len(repeat_counts), 3),
        'repeat_count_histogram': dict(Counter(repeat_counts)),
        'avg_repeated_last_digit_count': round(sum(digit_counts) / len(digit_counts), 3),
        'sample_size': len(with_prev),
    }


def concentration_profile(series, n=STRUCTURAL_WINDOW_SHORT):
    recent = series[:n]
    recent_concentrated = sum(1 for s in recent if s['state']['concentration_regime'] == 'concentrated')
    all_concentrated = sum(1 for s in series if s['state']['concentration_regime'] == 'concentrated')
    return {
        'recent_window': {
            'n': len(recent),
            'concentrated_count': recent_concentrated,
            'concentrated_pct': round(recent_concentrated / len(recent), 3) if recent else 0,
        },
        'all_time': {
            'n': len(series),
            'concentrated_count': all_concentrated,
            'concentrated_pct': round(all_concentrated / len(series), 3) if series else 0,
        },
    }

# ============================================================
# 9/10. CANDIDATE POOL AND PREDICTION LINES (steps 9-10)
# ============================================================

def build_candidate_pool(max_num, main_count, persistent, recurrence_data, freq_data, gap_data, decade_bins, current_numbers, neighbor_signal=None):
    """One shared {number: score} pool for all three lines, weighted to
    the user's stated Tier 1/2/3 hierarchy -- deliberately NOT
    pattern_predict()'s frequency/Markov/moon-weighted scorer, which is
    wrong for this model on both counts (frequency-dominant, moon-phase
    contaminated)."""
    pool = {n: 1.0 for n in range(1, max_num + 1)}  # small floor keeps every number reachable

    # Tier 1 (45%): decade-bin persistence, split with nearest-neighbor
    # follow-up frequency (which specific numbers followed the K
    # structurally similar historical draws) when there's enough of a
    # sample to say anything -- purely additive: falls back to the
    # original 45%-persistence-alone calibration when the neighbor signal
    # isn't available (e.g. draw_type='doubleplay', or too few of the K
    # matches have a followup drawing on record yet), so this never dilutes
    # the existing signal, only supplements it.
    has_neighbor = neighbor_signal and neighbor_signal['sample_size'] >= NEIGHBOR_MIN_SAMPLE_FOR_SIGNAL
    persistence_weight = 30.0 if has_neighbor else 45.0
    neighbor_weight = 15.0 if has_neighbor else 0.0

    persistence_by_num = {}
    for bidx, (lo, hi) in enumerate(decade_bins):
        info = persistent[bidx]
        score = (info[f'window_{STRUCTURAL_WINDOW_SHORT}']['coverage'] + info[f'window_{STRUCTURAL_WINDOW_LONG}']['coverage']) / 2
        for n in range(lo, hi + 1):
            persistence_by_num[n] = score
    max_pers = max(persistence_by_num.values()) if persistence_by_num else 1
    for n in range(1, max_num + 1):
        pool[n] += persistence_weight * (persistence_by_num.get(n, 0) / max_pers if max_pers else 0)

    if has_neighbor:
        neighbor_by_num = {t['number']: t['count'] for t in neighbor_signal['top_numbers']}
        max_neighbor = max(neighbor_by_num.values()) if neighbor_by_num else 1
        for n in range(1, max_num + 1):
            pool[n] += neighbor_weight * (neighbor_by_num.get(n, 0) / max_neighbor if max_neighbor else 0)

    # Tier 2 (30%): recurrence -- numbers from the previous draw get a
    # rate-matched (not zero, not favored) boost reflecting the empirical
    # recurrence rate, rather than being excluded as "just appeared" or
    # assumed to repeat because they're "due." Consecutive-pair legitimacy
    # (also Tier 2) is enforced structurally at generate_lines() via the
    # target regime check rather than as a per-number pool weight, since
    # "should this number be adjacent to another" isn't a single-number
    # property.
    expected_recurrence_rate = recurrence_data['avg_repeat_count'] / main_count if main_count else 0
    for n in current_numbers:
        if 1 <= n <= max_num:
            pool[n] += 30.0 * expected_recurrence_rate

    # Tier 3 (25%): frequency/gap as a tie-breaker only -- smallest
    # weight, never implying "overdue therefore likely."
    total_freq = freq_data['total']
    max_freq = max(total_freq.values()) if total_freq else 1
    for n in range(1, max_num + 1):
        f = (total_freq.get(n, 0) / max_freq) if max_freq else 0
        g = gap_data.get(n, {}).get('overdue_score', 0)
        g_norm = min(g / 3.0, 1.0)
        pool[n] += 25.0 * (0.7 * f + 0.3 * g_norm)

    return pool


def _tilt_pool(pool, hypothesis_label, decade_bins, persistent):
    if hypothesis_label == 'Primary':
        return dict(pool)
    tilted = dict(pool)
    for bidx, (lo, hi) in enumerate(decade_bins):
        info = persistent[bidx]
        cov = (info[f'window_{STRUCTURAL_WINDOW_SHORT}']['coverage'] + info[f'window_{STRUCTURAL_WINDOW_LONG}']['coverage']) / 2
        if hypothesis_label == 'Persistence':
            factor = 1.0 + cov          # further boost currently-persistent bins
        elif hypothesis_label == 'Variance':
            factor = 1.0 + (1.0 - cov)  # boost currently-rare bins
        else:
            factor = 1.0
        for n in range(lo, hi + 1):
            if n in tilted:
                tilted[n] *= factor
    return tilted


def _weighted_pick(pool, count):
    available = dict(pool)
    result = []
    for _ in range(count):
        total = sum(available.values())
        r = random.uniform(0, total)
        cum = 0
        for n, w in sorted(available.items()):
            cum += w
            if r <= cum:
                result.append(n)
                del available[n]
                break
    return sorted(result)


def generate_lines(pool, hypotheses, max_num, main_count, cutoffs, decade_bins, persistent, attempts=300):
    lines = []
    for hyp in hypotheses:
        tilted = _tilt_pool(pool, hyp['label'], decade_bins, persistent)
        target = hyp['target_regimes']
        best_score = -1
        tied = []  # every candidate seen so far at the current best_score
        for _ in range(attempts):
            candidate = _weighted_pick(tilted, main_count)
            state = structural_state(candidate, max_num)
            regimes = classify_regimes(state, cutoffs, main_count)
            matches = sum(1 for d, v in target.items() if regimes.get(d) == v)
            if matches > best_score:
                best_score = matches
                tied = [(candidate, state, regimes)]
            elif matches == best_score:
                tied.append((candidate, state, regimes))
        # Multiple target regimes can reinforce the same direction at once
        # (e.g. low sum + low-heavy + concentrated + multiple-consecutive all
        # point toward "small clustered numbers"), and the fastest way for
        # random sampling to satisfy all of them simultaneously is an
        # extreme, tightly-packed cluster (e.g. 1-2-3-5-59) -- which reads as
        # a suspicious pattern even though it's a legitimate boolean match.
        # Keeping only the first candidate to reach a score would lock in
        # whichever extreme the random walk happened to hit first; picking
        # the widest-spread candidate among every tie at the best score
        # keeps the same target regimes while favoring a more representative
        # (less artificially bunched) instance of that structure.
        best, best_state, best_regimes = max(tied, key=lambda t: t[1]['spread'])
        lines.append({
            'hypothesis': hyp['label'],
            'name': hyp['name'],
            'numbers': best,
            'sum': best_state['sum'],
            'odd_count': best_state['odd_count'],
            'even_count': best_state['even_count'],
            'low_count': best_state['low_count'],
            'high_count': best_state['high_count'],
            'decade_histogram': best_state['decade_histogram'],
            'consecutive_pairs': best_state['consecutive_pairs'],
            'target_regimes': target,
            'achieved_regimes': best_regimes,
            'matched_dimensions': best_score,
            'total_dimensions': len(target),
            'why': hyp['rationale'],
            'confidence': hyp['confidence'],
        })
    return lines

# ============================================================
# SCHEDULING
# ============================================================

def next_scheduled_draw_date(game, after_date):
    cfg = get_config(game)
    weekdays = cfg.get('draw_weekdays', [2, 5])
    d = after_date + timedelta(days=1)
    while d.weekday() not in weekdays:
        d += timedelta(days=1)
    return d

# ============================================================
# MASTER ORCHESTRATOR
# ============================================================

def tsf_forecast(game='powerball', draw_type='main'):
    cfg = get_config(game)
    max_num = cfg['max_num']
    main_count = cfg['main_count']
    draw_type = draw_type if (draw_type == 'doubleplay' and cfg['doubleplay_game_value']) else 'main'

    draws = load_draws(game, draw_type=draw_type)
    if not draws:
        return {'error': 'No historical data found'}

    decade_bins = _decade_bins(max_num)
    series, cutoffs = build_structural_series(draws, max_num, main_count)

    current = current_structural_state(series)
    persistent = persistent_zones(series, decade_bins)
    transitions = structural_transitions(series)
    lookup = transition_lookup(transitions, current['regimes'], series)
    neighbor_signal = neighbor_structural_signal(game, max_num, main_count, cutoffs) if draw_type == 'main' else None
    hypotheses = build_hypotheses(current['regimes'], lookup, cfg, neighbor_signal)

    freq_data = frequency_analysis(draws)
    gap_data = gap_analysis(draws, max_num)
    recurrence = recurrence_profile(series)
    concentration_prof = concentration_profile(series)

    pool = build_candidate_pool(max_num, main_count, persistent, recurrence, freq_data, gap_data, decade_bins, current['numbers'], neighbor_signal)
    lines = generate_lines(pool, hypotheses, max_num, main_count, cutoffs, decade_bins, persistent)

    as_of_date = datetime.strptime(draws[0]['date'], '%Y-%m-%d').date()
    target_date = next_scheduled_draw_date(game, as_of_date)

    return {
        'game': game,
        'draw_type': draw_type,
        'max_num': max_num,
        'main_count': main_count,
        'total_draws': len(draws),
        'as_of_draw_date': str(as_of_date),
        'target_draw_date': str(target_date),
        'current_state': current,
        'persistent_zones': persistent,
        'decade_bins': decade_bins,
        'transitions': lookup,
        'hypotheses': hypotheses,
        'lines': lines,
        'recurrence_profile': recurrence,
        'concentration_profile': concentration_prof,
        'sum_regime_cutoffs': list(cutoffs),
        'neighbor_signal': neighbor_signal,
        'model_version': MODEL_VERSION,
    }

# ============================================================
# SCORING (step 11) -- multi-dimensional, not just exact-number hits
# ============================================================

def score_forecast(lines, actual_numbers, max_num, main_count, cutoffs,
                    recurrence_expected=None, prev_actual_numbers=None):
    actual_state = structural_state(actual_numbers, max_num)
    actual_regimes = classify_regimes(actual_state, cutoffs, main_count)

    scorecards = []
    for line in lines:
        exact_hits = len(set(line['numbers']) & set(actual_numbers))
        target = line['target_regimes']
        dim_correct = {d: (actual_regimes.get(d) == v) for d, v in target.items()}

        line_hist, actual_hist = line['decade_histogram'], actual_state['decade_histogram']
        range_ok = all(abs(a - b) <= 1 for a, b in zip(line_hist, actual_hist))
        transition_regime_correct = all(dim_correct.get(d) for d in CORE_DIMS if d in dim_correct)

        scorecards.append({
            'hypothesis': line['hypothesis'],
            'exact_hits': exact_hits,
            'range_distribution_correct': range_ok,
            'parity_correct': dim_correct.get('parity_regime'),
            'low_high_correct': dim_correct.get('low_high_regime'),
            'sum_regime_correct': dim_correct.get('sum_regime'),
            'concentration_correct': dim_correct.get('concentration_regime'),
            'consecutive_regime_correct': dim_correct.get('consecutive_regime'),
            'transition_regime_correct': transition_regime_correct,
            'assumptions_supported': [d for d, ok in dim_correct.items() if ok],
            'assumptions_contradicted': [d for d, ok in dim_correct.items() if not ok],
        })

    recurrence_correct = None
    if prev_actual_numbers is not None and recurrence_expected is not None:
        deltas = previous_draw_deltas(actual_numbers, prev_actual_numbers)
        recurrence_correct = abs(deltas['repeat_count'] - recurrence_expected) <= 1

    return {
        'actual_numbers': sorted(actual_numbers),
        'actual_state': actual_state,
        'actual_regimes': actual_regimes,
        'recurrence_correct': recurrence_correct,
        'lines': scorecards,
    }


def track_record_summary(scored_rows, model_version=MODEL_VERSION):
    """Aggregates scorecard JSON across scored rows -- per-dimension hit
    rate over time, per hypothesis label. Stands in for step 12's
    automatic reweighting: this is what a human reviews to decide by eye
    whether/how the model should change."""
    dims = ['range_distribution_correct', 'parity_correct', 'low_high_correct',
            'sum_regime_correct', 'concentration_correct', 'consecutive_regime_correct',
            'transition_regime_correct']

    rows = [r for r in scored_rows if r.get('model_version') == model_version and r.get('scorecard')]
    if not rows:
        return {'model_version': model_version, 'scored_count': 0, 'by_hypothesis': {}, 'recurrence_correct_pct': None}

    by_hyp = {}
    for r in rows:
        for line_score in r['scorecard']['lines']:
            label = line_score['hypothesis']
            agg = by_hyp.setdefault(label, {'count': 0, 'exact_hits_total': 0, **{d: 0 for d in dims}})
            agg['count'] += 1
            agg['exact_hits_total'] += line_score.get('exact_hits', 0)
            for d in dims:
                if line_score.get(d):
                    agg[d] += 1

    by_hyp_pct = {}
    for label, agg in by_hyp.items():
        n = agg['count']
        by_hyp_pct[label] = {
            'scored_count': n,
            'avg_exact_hits': round(agg['exact_hits_total'] / n, 2) if n else 0,
            **{f'{d}_pct': round(agg[d] / n * 100, 1) for d in dims}
        }

    recurrence_checks = [r['scorecard'].get('recurrence_correct') for r in rows if r['scorecard'].get('recurrence_correct') is not None]
    recurrence_pct = round(sum(1 for c in recurrence_checks if c) / len(recurrence_checks) * 100, 1) if recurrence_checks else None

    return {
        'model_version': model_version,
        'scored_count': len(rows),
        'by_hypothesis': by_hyp_pct,
        'recurrence_correct_pct': recurrence_pct,
    }


if __name__ == '__main__':
    import json
    result = tsf_forecast('powerball')
    print(json.dumps(result, indent=2, default=str)[:3000])
