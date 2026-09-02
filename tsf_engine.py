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


def _decade_baseline_rate(series, bidx, value):
    total = len(series)
    if not total:
        return 0
    count = sum(1 for s in series if _count_bucket(s['state']['decade_histogram'][bidx]) == value)
    return count / total


def decade_transition_lookup(transitions_result, current_decade_buckets, series):
    """Same confidence-labeling approach as transition_lookup(), but for
    each decade bin's own none/few/many bucket -- the "few 20s -> many
    20s" style transitions from the original spec, which structural_
    transitions() already computed but nothing ever surfaced or used
    until now."""
    decade_result = transitions_result['decade_bins']
    lookup = {}
    for bidx, frm_val in current_decade_buckets.items():
        frm_row = decade_result.get(bidx, {}).get(frm_val)
        entry = {'from': frm_val, 'sample_size': 0, 'to_values': {}}
        if frm_row:
            entry['sample_size'] = frm_row['total']
            for to_val, stats in frm_row['to'].items():
                baseline = _decade_baseline_rate(series, bidx, to_val)
                label = confidence_label(frm_row['total'], stats['count'], baseline)
                entry['to_values'][to_val] = {
                    'count': stats['count'],
                    'pct': stats['pct'],
                    'baseline_pct': round(baseline, 4),
                    'lift': round(stats['pct'] / baseline, 3) if baseline > 0 else None,
                    'confidence': label,
                }
        lookup[bidx] = entry
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


# Validated per-game via a held-out backtest (12 months of history,
# point-in-time-correct so no lookahead, derived on the older half and
# confirmed on the newer half it never saw) -- matches v1's approach
# after the same methodology showed Florida Lotto and Jackpot Triple
# Play needed different calibrations too. Powerball and Mega Millions
# don't agree with each other either.
#
# powerball: sum_regime was re-tested after fixing a reversed-chronology
# bug in the calibration backtest (the aggregate baseline was measuring
# what preceded a regime, not what followed it). Corrected, it's a dead
# heat in-sample (29% vs 29% train, n=52) and a tiny, sub-threshold edge
# out-of-sample (33% vs 31%, n=45) -- no real signal, so removed. Every
# other dimension has too few disagreement cases (0-4) to test at all.
#
# megamillions: sum_regime flipped direction between train (aggregate
# favored, 47% vs 21%) and test (neighbor favored, 39% vs 9%) -- a sign
# of noise, not signal, so left out. Every other dimension also had too
# few disagreement cases (0-4) to test. Left empty rather than force an
# unvalidated preference; re-run the backtest as more data accumulates.
NEIGHBOR_PREFERRED_DIMS_BY_GAME = {
    'powerball': set(),
    'megamillions': set(),
}


def _primary_target_with_neighbor(d, frm_val, entry, neighbor_signal, game):
    agg_tgt = _primary_target(entry, frm_val)
    preferred_dims = NEIGHBOR_PREFERRED_DIMS_BY_GAME.get(game, set())
    if d not in preferred_dims:
        return agg_tgt
    neighbor_val, _ = _neighbor_top(neighbor_signal, d)
    if neighbor_val is not None and neighbor_val != agg_tgt:
        if neighbor_val in entry['to_values'] and entry['to_values'][neighbor_val]['count'] > 0:
            return neighbor_val
    return agg_tgt


def _variance_target(dim, frm_val, entry):
    direct = REVERSE_MAP.get(dim, {}).get(frm_val)
    to_values = entry['to_values']
    if direct and direct in to_values and to_values[direct]['count'] > 0:
        return direct
    candidates = {v: s['count'] for v, s in to_values.items() if v != frm_val}
    if candidates:
        return max(candidates, key=candidates.get)
    return direct or frm_val


# Validated per-game via the same held-out backtest methodology as v1
# (12 months, point-in-time-correct, train derived / test held out),
# comparing this game's generic reversal against preferring the neighbor
# signal's own top pick whenever it differs from the generic choice and
# has real historical precedent. The margins here are dramatically
# larger than Primary's calibration ever showed -- the generic reversal
# is a much cruder heuristic than "what actually happened after draws
# shaped like this one."
#
# powerball: neighbor wins consistently and by large margins on
# parity_regime (63%/55% vs 19%/18%), low_high_regime (73%/74% vs
# 14%/8%), concentration_regime (84%/81% vs 16%/19%), consecutive_regime
# (71%/71% vs 2%/2%). sum_regime was re-tested after fixing a reversed-
# chronology bug in the calibration backtest itself (the aggregate
# baseline was measuring what preceded a regime, not what followed it)
# -- with that corrected, the train-half margin drops to 3.5pp (below
# the 5pp threshold), so it no longer holds up and was removed.
#
# megamillions: same pattern except sum_regime, which was a dead tie in
# both halves (26%/26% train, 34%/31% test) -- no benefit either way, so
# excluded. parity_regime (63%/57% vs 15%/18%), low_high_regime
# (61%/68% vs 10%/16%), concentration_regime (81%/79% vs 19%/21%),
# consecutive_regime (76%/84% vs 3%/0%) all won consistently.
VARIANCE_NEIGHBOR_DIMS_BY_GAME = {
    'powerball': {'parity_regime', 'low_high_regime', 'concentration_regime', 'consecutive_regime'},
    'megamillions': {'parity_regime', 'low_high_regime', 'concentration_regime', 'consecutive_regime'},
}


# Validated via a held-out backtest specifically for Powerball Double Play
# (12mo, point-in-time, train/test split, comparing the generic reversal
# against the plain aggregate/whole-history mode -- the same value Primary
# would pick): the reversal is dramatically worse than the aggregate on
# every dimension with enough disagreement cases to test. parity_regime
# (69%/74% aggregate vs 14%/14% reversal), low_high_regime (62%/65% vs
# 12%/19%), concentration_regime (78%/65% vs 22%/35%), consecutive_regime
# (65%/73% vs 2%/4%) -- all consistent across both halves, far beyond
# noise. sum_regime had too few disagreement cases (n=3-10) to test.
#
# Double Play never gets a neighbor signal (only computed for the main
# stream), so Variance there was always the raw generic reversal alone --
# and that alone appears to actively fight against how Double Play's
# regimes actually behave (sticky/persistent rather than mean-reverting),
# unlike main-stream Powerball where reversal (optionally neighbor-
# informed) works fine. On these dimensions Variance now targets the same
# regime Primary does for Double Play -- a real, data-driven collapse of
# the "reversal scenario" concept where it demonstrably doesn't apply, not
# a bug. Keyed by (game, draw_type) since this doesn't touch main-stream
# Powerball at all.
VARIANCE_AGGREGATE_FALLBACK_DIMS_BY_GAME_DRAWTYPE = {
    ('powerball', 'doubleplay'): {'parity_regime', 'low_high_regime', 'concentration_regime', 'consecutive_regime'},
}


def _variance_target_with_neighbor_impl(d, frm_val, entry, neighbor_signal, game, draw_type='main'):
    if d in VARIANCE_AGGREGATE_FALLBACK_DIMS_BY_GAME_DRAWTYPE.get((game, draw_type), set()):
        return _primary_target(entry, frm_val)
    generic_tgt = _variance_target(d, frm_val, entry)
    preferred_dims = VARIANCE_NEIGHBOR_DIMS_BY_GAME.get(game, set())
    if d not in preferred_dims:
        return generic_tgt
    neighbor_val, _ = _neighbor_top(neighbor_signal, d)
    if neighbor_val is not None and neighbor_val != generic_tgt:
        if neighbor_val in entry['to_values'] and entry['to_values'][neighbor_val]['count'] > 0:
            return neighbor_val
    return generic_tgt


def _weakest_link(confs):
    vals = [confs[d] for d in CORE_DIMS if d in confs]
    return min(vals, key=lambda c: CONF_RANK[c]) if vals else 'Low'


NEIGHBOR_AGREEMENT_BONUS = {'Low': 'Medium', 'Medium': 'High', 'High': 'High'}


def build_hypotheses(current_regimes, lookup, cfg, neighbor_signal=None, game=None, draw_type='main'):
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
        return _variance_target_with_neighbor_impl(d, frm, e, neighbor_signal, game, draw_type)

    hypotheses.append(_line('Primary', 'Most likely transition', lambda d, frm, e: _primary_target_with_neighbor(d, frm, e, neighbor_signal, game)))
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

DECADE_TRANSITION_BUCKET_FACTOR = {'many': 1.15, 'few': 1.0, 'none': 0.85}


def build_candidate_pool(max_num, main_count, persistent, recurrence_data, freq_data, gap_data, decade_bins, current_numbers, neighbor_signal=None, decade_lookup=None):
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

    # Decade-bin transition data ("few 20s -> many 20s") modestly
    # modulates each bin's persistence score -- unlike the neighbor
    # signal and the Primary/Variance calibration above, this specific
    # integration hasn't been backtested yet, so the adjustment is
    # deliberately small and bounded (+-15%) rather than a full new
    # weighted tier, and only nudges a bin toward/away from its already-
    # computed persistence score rather than overriding it.
    decade_lookup = decade_lookup or {}

    persistence_by_num = {}
    for bidx, (lo, hi) in enumerate(decade_bins):
        info = persistent[bidx]
        score = (info[f'window_{STRUCTURAL_WINDOW_SHORT}']['coverage'] + info[f'window_{STRUCTURAL_WINDOW_LONG}']['coverage']) / 2
        bin_transition = decade_lookup.get(bidx, {}).get('to_values')
        if bin_transition:
            predicted_bucket = max(bin_transition, key=lambda v: bin_transition[v]['pct'])
            score *= DECADE_TRANSITION_BUCKET_FACTOR.get(predicted_bucket, 1.0)
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


def generate_lines(pool, hypotheses, max_num, main_count, cutoffs, decade_bins, persistent, series, attempts=300):
    # Real historical average spread for this game -- the tie-break below
    # picks whichever tied candidate's spread is closest to this, not the
    # widest one (see the comment at the tie-break for why).
    hist_avg_spread = sum(s['state']['spread'] for s in series) / len(series) if series else (max_num - 1) * 4 / 6

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
        # whichever extreme the random walk happened to hit first.
        #
        # Picking the WIDEST-spread tied candidate (the original approach)
        # overcorrected into a different, worse bias: since spread = max -
        # min, "widest" is mechanically maximized by hugging the boundaries
        # of the range, so line 1 was landing on 1 as much as ~35-55% of the
        # time on a 1-69 game (vs. ~7% expected under uniform chance) --
        # confirmed empirically, not a guess. Picking the candidate closest
        # to the MEDIAN of the tied set alone wasn't enough either (still
        # ~17-37%): the tied set itself skews wide, since satisfying every
        # target regime simultaneously is often easier for a spread-out
        # combo. Anchoring to the REAL historical average spread for this
        # game instead (hist_avg_spread, computed once above) grounds the
        # choice in what an actual draw looks like rather than in whatever
        # this particular random batch of 300 attempts happened to produce
        # -- confirmed empirically to bring the boundary bias down close to
        # chance level (~0-20%, vs. the original ~35-55%).
        best, best_state, best_regimes = min(tied, key=lambda t: abs(t[1]['spread'] - hist_avg_spread))
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
    current_decade_buckets = {i: _count_bucket(c) for i, c in enumerate(current['state']['decade_histogram'])}
    decade_lookup = decade_transition_lookup(transitions, current_decade_buckets, series)
    neighbor_signal = neighbor_structural_signal(game, max_num, main_count, cutoffs) if draw_type == 'main' else None
    hypotheses = build_hypotheses(current['regimes'], lookup, cfg, neighbor_signal, game, draw_type)

    freq_data = frequency_analysis(draws)
    gap_data = gap_analysis(draws, max_num)
    recurrence = recurrence_profile(series)
    concentration_prof = concentration_profile(series)

    pool = build_candidate_pool(max_num, main_count, persistent, recurrence, freq_data, gap_data, decade_bins, current['numbers'], neighbor_signal, decade_lookup)
    lines = generate_lines(pool, hypotheses, max_num, main_count, cutoffs, decade_bins, persistent, series)

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
        'decade_transitions': decade_lookup,
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

# ============================================================
# CALIBRATION REPORT -- re-runs the same held-out backtest that
# originally validated NEIGHBOR_PREFERRED_DIMS_BY_GAME and
# VARIANCE_NEIGHBOR_DIMS_BY_GAME, so the calibration can be refreshed as
# more data accumulates without a one-off script. Point-in-time-correct
# (no lookahead): each target draw's neighbor search only considers
# draws strictly before it, matching what the live model would have
# actually known at the time.
# ============================================================

MIN_DIFFER_SAMPLE_PER_HALF = 20   # below this per half, a dimension's verdict is "insufficient data"
CALIBRATION_MARGIN = 0.05          # need >=5pp edge in BOTH halves to call it a real, not noisy, preference


def _calibration_backtest_pass(chrono, date_to_idx, max_num, main_count, indices, k=NEIGHBOR_K):
    """One pass over `indices` (positions into `chrono`, oldest-first).
    Only counts a "differ" case -- where the neighbor-informed pick would
    actually have chosen something different from the baseline (aggregate
    for Primary, generic reversal for Variance) -- since agreement cases
    don't help decide which one to trust when they conflict."""
    from match_engine import compute_features, similarity_score

    primary_differ = {d: {'n': 0, 'agg': 0, 'nbr': 0} for d in DIMENSIONS}
    variance_differ = {d: {'n': 0, 'gen': 0, 'nbr': 0} for d in DIMENSIONS}
    n_tested = 0

    for ti in indices:
        target_draw = chrono[ti]
        anchor_draw = chrono[ti - 1]
        history_before = chrono[:ti]
        if len(history_before) < 200:
            continue
        n_tested += 1

        cutoffs = sum_regime_cutoffs(history_before)
        # structural_transitions() assumes most-recent-first order (series[i]
        # later than series[i+1]), but history_before is chronological
        # (oldest-first) -- reverse it here or the transition table comes out
        # backwards (what preceded a regime, not what followed it).
        series, _ = build_structural_series(list(reversed(history_before)), max_num, main_count)
        anchor_regimes = classify_regimes(structural_state(anchor_draw['numbers'], max_num), cutoffs, main_count)
        transitions = structural_transitions(series)
        lookup = transition_lookup(transitions, anchor_regimes, series)

        anchor_features = compute_features(anchor_draw['numbers'], max_num)
        candidates = history_before[:-1]
        scored = sorted(
            ((similarity_score(anchor_features, compute_features(c['numbers'], max_num), max_num), c)
             for c in candidates),
            key=lambda x: -x[0],
        )
        regime_tally = {d: Counter() for d in DIMENSIONS}
        for _, m in scored[:k]:
            midx = date_to_idx[m['date']]
            if midx + 1 >= ti:
                continue
            followup = chrono[midx + 1]
            fregimes = classify_regimes(structural_state(followup['numbers'], max_num), cutoffs, main_count)
            for d in DIMENSIONS:
                regime_tally[d][fregimes[d]] += 1
        neighbor_signal = {
            'sample_size': max((sum(c.values()) for c in regime_tally.values() if c), default=0),
            'by_dimension': {
                d: {v: {'count': c, 'pct': c / sum(cnt.values())} for v, c in cnt.items()}
                for d, cnt in regime_tally.items() if cnt
            },
        }

        target_regimes = classify_regimes(structural_state(target_draw['numbers'], max_num), cutoffs, main_count)

        for d in DIMENSIONS:
            frm = anchor_regimes[d]
            entry = lookup.get(d, {'to_values': {}})
            neighbor_val, _ = _neighbor_top(neighbor_signal, d)

            agg_tgt = _primary_target(entry, frm)
            nbr_tgt_primary = agg_tgt
            if neighbor_val is not None and neighbor_val != agg_tgt:
                if neighbor_val in entry['to_values'] and entry['to_values'][neighbor_val]['count'] > 0:
                    nbr_tgt_primary = neighbor_val
            if agg_tgt != nbr_tgt_primary:
                primary_differ[d]['n'] += 1
                if agg_tgt == target_regimes[d]:
                    primary_differ[d]['agg'] += 1
                if nbr_tgt_primary == target_regimes[d]:
                    primary_differ[d]['nbr'] += 1

            gen_tgt = _variance_target(d, frm, entry)
            nbr_tgt_variance = gen_tgt
            if neighbor_val is not None and neighbor_val != gen_tgt:
                if neighbor_val in entry['to_values'] and entry['to_values'][neighbor_val]['count'] > 0:
                    nbr_tgt_variance = neighbor_val
            if gen_tgt != nbr_tgt_variance:
                variance_differ[d]['n'] += 1
                if gen_tgt == target_regimes[d]:
                    variance_differ[d]['gen'] += 1
                if nbr_tgt_variance == target_regimes[d]:
                    variance_differ[d]['nbr'] += 1

    return n_tested, primary_differ, variance_differ


def _calibration_verdict(train_stat, test_stat, baseline_key, current_dims, dim):
    tn, te = train_stat['n'], test_stat['n']
    if tn < MIN_DIFFER_SAMPLE_PER_HALF or te < MIN_DIFFER_SAMPLE_PER_HALF:
        verdict = 'insufficient_data'
    else:
        train_base_pct = train_stat[baseline_key] / tn
        train_nbr_pct = train_stat['nbr'] / tn
        test_base_pct = test_stat[baseline_key] / te
        test_nbr_pct = test_stat['nbr'] / te
        train_favors_nbr = (train_nbr_pct - train_base_pct) >= CALIBRATION_MARGIN
        train_favors_base = (train_base_pct - train_nbr_pct) >= CALIBRATION_MARGIN
        test_favors_nbr = (test_nbr_pct - test_base_pct) >= CALIBRATION_MARGIN
        test_favors_base = (test_base_pct - test_nbr_pct) >= CALIBRATION_MARGIN
        if train_favors_nbr and test_favors_nbr:
            verdict = 'favors_neighbor'
        elif train_favors_base and test_favors_base:
            verdict = 'favors_baseline'
        else:
            verdict = 'inconsistent'

    currently_enabled = dim in current_dims
    if verdict == 'favors_neighbor':
        recommendation = 'keep' if currently_enabled else 'add'
    elif verdict in ('favors_baseline', 'inconsistent'):
        recommendation = 'remove' if currently_enabled else 'no_change'
    else:
        recommendation = 'no_change'

    return {
        'verdict': verdict,
        'currently_enabled': currently_enabled,
        'recommendation': recommendation,
        'train': {
            'n': tn,
            'baseline_pct': round(train_stat[baseline_key] / tn * 100, 1) if tn else None,
            'neighbor_pct': round(train_stat['nbr'] / tn * 100, 1) if tn else None,
        },
        'test': {
            'n': te,
            'baseline_pct': round(test_stat[baseline_key] / te * 100, 1) if te else None,
            'neighbor_pct': round(test_stat['nbr'] / te * 100, 1) if te else None,
        },
    }


def calibration_report(game, draw_type='main', months=12, k=NEIGHBOR_K):
    cfg = get_config(game)
    max_num, main_count = cfg['max_num'], cfg['main_count']
    all_draws = load_draws(game, draw_type=draw_type)
    if not all_draws:
        return {'error': 'No historical data found'}
    chrono = list(reversed(all_draws))
    date_to_idx = {d['date']: idx for idx, d in enumerate(chrono)}

    cutoff_date = datetime.now().date() - timedelta(days=30 * months)
    target_indices = [
        i for i, d in enumerate(chrono)
        if datetime.strptime(d['date'], '%Y-%m-%d').date() >= cutoff_date and i > 50
    ]
    if len(target_indices) < MIN_DIFFER_SAMPLE_PER_HALF * 2:
        return {'error': f"Only {len(target_indices)} draws in the last {months} months -- not enough for a meaningful backtest."}

    mid = len(target_indices) // 2
    train_idx, test_idx = target_indices[:mid], target_indices[mid:]

    n_train, primary_train, variance_train = _calibration_backtest_pass(chrono, date_to_idx, max_num, main_count, train_idx, k)
    n_test, primary_test, variance_test = _calibration_backtest_pass(chrono, date_to_idx, max_num, main_count, test_idx, k)

    current_primary_dims = NEIGHBOR_PREFERRED_DIMS_BY_GAME.get(game, set())
    current_variance_dims = VARIANCE_NEIGHBOR_DIMS_BY_GAME.get(game, set())

    primary_findings, variance_findings = {}, {}
    any_changes = False
    for d in DIMENSIONS:
        pf = _calibration_verdict(primary_train[d], primary_test[d], 'agg', current_primary_dims, d)
        vf = _calibration_verdict(variance_train[d], variance_test[d], 'gen', current_variance_dims, d)
        primary_findings[d] = pf
        variance_findings[d] = vf
        if pf['recommendation'] in ('add', 'remove') or vf['recommendation'] in ('add', 'remove'):
            any_changes = True

    return {
        'game': game,
        'draw_type': draw_type,
        'months': months,
        'generated_at': datetime.now().isoformat(),
        'n_train_draws': n_train,
        'n_test_draws': n_test,
        'primary': primary_findings,
        'variance': variance_findings,
        'current_config': {
            'primary_preferred_dims': sorted(current_primary_dims),
            'variance_preferred_dims': sorted(current_variance_dims),
        },
        'any_changes_suggested': any_changes,
    }

# ============================================================
# LINE BACKTEST REPORT -- unlike calibration_report() (which only checks
# whether a REGIME target was right), this runs the full pipeline --
# structural state, transitions, neighbor signal, hypotheses, candidate
# pool, generate_lines() -- exactly as tsf_forecast() would for each
# historical anchor draw, then scores the actual generated 5/6-number
# lines against the real next draw with score_forecast(). Answers "would
# this app's generated numbers have actually matched past drawings,"
# point-in-time-correct (no lookahead) and split train/test like the
# calibration report, so a real edge is distinguished from noise.
# ============================================================

LINE_BACKTEST_MIN_HISTORY = 200  # same floor as _calibration_backtest_pass


def _point_in_time_neighbor_signal(chrono, date_to_idx, ti, max_num, main_count, k=NEIGHBOR_K):
    """Point-in-time-correct stand-in for neighbor_structural_signal() --
    that function searches match_engine's LIVE full-history table, which
    would leak future draws into a backtest anchored in the past. Mirrors
    _calibration_backtest_pass()'s similarity search, but also tallies
    per-number follow-up frequency (top_numbers), which
    build_candidate_pool() needs and the calibration pass never did."""
    from match_engine import compute_features, similarity_score

    anchor_draw = chrono[ti - 1]
    history_before = chrono[:ti]
    cutoffs = sum_regime_cutoffs(history_before)
    anchor_features = compute_features(anchor_draw['numbers'], max_num)
    candidates = history_before[:-1]
    scored = sorted(
        ((similarity_score(anchor_features, compute_features(c['numbers'], max_num), max_num), c)
         for c in candidates),
        key=lambda x: -x[0],
    )

    regime_tally = {d: Counter() for d in DIMENSIONS}
    number_counts = Counter()
    sample_size = 0
    for _, m in scored[:k]:
        midx = date_to_idx[m['date']]
        if midx + 1 >= ti:
            continue
        followup = chrono[midx + 1]
        sample_size += 1
        fregimes = classify_regimes(structural_state(followup['numbers'], max_num), cutoffs, main_count)
        for d in DIMENSIONS:
            regime_tally[d][fregimes[d]] += 1
        for n in followup['numbers']:
            number_counts[n] += 1

    by_dimension = {}
    for d in DIMENSIONS:
        total = sum(regime_tally[d].values())
        if total:
            by_dimension[d] = {v: {'count': c, 'pct': round(c / total, 4)} for v, c in regime_tally[d].items()}

    return {
        'sample_size': sample_size,
        'k_requested': k,
        'by_dimension': by_dimension,
        'top_numbers': [{'number': n, 'count': c} for n, c in number_counts.most_common(15)],
    }


def _chance_expected_hits(max_num, main_count):
    """Expected exact number-hits for a uniformly random line of
    main_count distinct numbers vs. the actual draw -- the baseline
    every hypothesis's avg_exact_hits should be measured against, since
    even a hypothesis with zero real skill will land some hits by chance
    alone on a small max_num."""
    return round(main_count * (main_count / max_num), 3)


DIM_SCORE_KEYS = (
    ('sum_regime', 'sum_regime_correct'), ('parity_regime', 'parity_correct'),
    ('low_high_regime', 'low_high_correct'), ('concentration_regime', 'concentration_correct'),
    ('consecutive_regime', 'consecutive_regime_correct'),
)


def _line_backtest_pass(chrono, date_to_idx, cfg, game, indices, k=NEIGHBOR_K, draw_type='main', samples_per_draw=10):
    """generate_lines() samples randomly (_weighted_pick uses random.uniform),
    so a single generated line per anchor draw is one noisy realization --
    re-running the report gave visibly different avg_exact_hits each time.
    Everything upstream of generate_lines() (transitions, neighbor signal,
    candidate pool) is deterministic given the anchor, so it's computed once
    per anchor; only the cheap random line-generation step repeats
    samples_per_draw times and the hit-rate is averaged, estimating the
    expected value instead of one draw from it. dims_correct is unaffected
    by this randomness (target_regimes never changes across samples) so
    it's only computed once per anchor either way."""
    max_num, main_count = cfg['max_num'], cfg['main_count']
    decade_bins = _decade_bins(max_num)

    stats = {label: {'n': 0, 'exact_hits': 0, 'dims_correct': {d: 0 for d in DIMENSIONS}, 'range_correct': 0}
              for label in ('Primary', 'Persistence', 'Variance')}
    n_tested = 0

    for ti in indices:
        target_draw = chrono[ti]
        anchor_draw = chrono[ti - 1]
        history_before = chrono[:ti]
        if len(history_before) < LINE_BACKTEST_MIN_HISTORY:
            continue
        n_tested += 1

        cutoffs = sum_regime_cutoffs(history_before)
        series, _ = build_structural_series(list(reversed(history_before)), max_num, main_count)
        current_regimes = classify_regimes(structural_state(anchor_draw['numbers'], max_num), cutoffs, main_count)
        transitions = structural_transitions(series)
        lookup = transition_lookup(transitions, current_regimes, series)
        current_decade_buckets = {i: _count_bucket(c) for i, c in enumerate(structural_state(anchor_draw['numbers'], max_num)['decade_histogram'])}
        decade_lookup = decade_transition_lookup(transitions, current_decade_buckets, series)

        neighbor_signal = (
            _point_in_time_neighbor_signal(chrono, date_to_idx, ti, max_num, main_count, k)
            if draw_type == 'main' else None
        )
        hypotheses = build_hypotheses(current_regimes, lookup, cfg, neighbor_signal, game, draw_type)

        freq_data = frequency_analysis(history_before)
        gap_data = gap_analysis(history_before, max_num)
        persistent = persistent_zones(series, decade_bins)
        recurrence = recurrence_profile(series)

        pool = build_candidate_pool(max_num, main_count, persistent, recurrence, freq_data, gap_data,
                                     decade_bins, anchor_draw['numbers'], neighbor_signal, decade_lookup)

        exact_hits_sum = {label: 0 for label in stats}
        range_correct_sum = {label: 0 for label in stats}
        dims_correct_this_anchor = {}
        for sample_i in range(samples_per_draw):
            lines = generate_lines(pool, hypotheses, max_num, main_count, cutoffs, decade_bins, persistent, series)
            scorecard = score_forecast(lines, target_draw['numbers'], max_num, main_count, cutoffs)
            for lc in scorecard['lines']:
                label = lc['hypothesis']
                exact_hits_sum[label] += lc['exact_hits']
                range_correct_sum[label] += 1 if lc['range_distribution_correct'] else 0
                if sample_i == 0:
                    dims_correct_this_anchor[label] = {d: lc[key] for d, key in DIM_SCORE_KEYS}

        for label, s in stats.items():
            s['n'] += 1
            s['exact_hits'] += exact_hits_sum[label] / samples_per_draw
            s['range_correct'] += range_correct_sum[label] / samples_per_draw
            for d, ok in dims_correct_this_anchor[label].items():
                if ok:
                    s['dims_correct'][d] += 1

    return n_tested, stats


def _summarize_line_stats(stats, max_num, main_count):
    chance = _chance_expected_hits(max_num, main_count)
    out = {}
    for label, s in stats.items():
        n = s['n']
        out[label] = {
            'n': n,
            'avg_exact_hits': round(s['exact_hits'] / n, 3) if n else None,
            'chance_expected_hits': chance,
            'lift_vs_chance': round((s['exact_hits'] / n) / chance, 3) if n and chance else None,
            'range_distribution_pct': round(s['range_correct'] / n * 100, 1) if n else None,
            'dims_correct_pct': {d: round(c / n * 100, 1) if n else None for d, c in s['dims_correct'].items()},
        }
    return out


def line_backtest_report(game, draw_type='main', months=12, k=NEIGHBOR_K, samples_per_draw=10):
    cfg = get_config(game)
    max_num, main_count = cfg['max_num'], cfg['main_count']
    all_draws = load_draws(game, draw_type=draw_type)
    if not all_draws:
        return {'error': 'No historical data found'}
    chrono = list(reversed(all_draws))
    date_to_idx = {d['date']: idx for idx, d in enumerate(chrono)}

    cutoff_date = datetime.now().date() - timedelta(days=30 * months)
    target_indices = [
        i for i, d in enumerate(chrono)
        if datetime.strptime(d['date'], '%Y-%m-%d').date() >= cutoff_date and i > LINE_BACKTEST_MIN_HISTORY
    ]
    if len(target_indices) < 20:
        return {'error': f"Only {len(target_indices)} draws in the last {months} months -- not enough for a meaningful backtest."}

    mid = len(target_indices) // 2
    train_idx, test_idx = target_indices[:mid], target_indices[mid:]

    n_train, train_stats = _line_backtest_pass(chrono, date_to_idx, cfg, game, train_idx, k, draw_type, samples_per_draw)
    n_test, test_stats = _line_backtest_pass(chrono, date_to_idx, cfg, game, test_idx, k, draw_type, samples_per_draw)

    return {
        'game': game,
        'draw_type': draw_type,
        'months': months,
        'samples_per_draw': samples_per_draw,
        'generated_at': datetime.now().isoformat(),
        'n_train_draws': n_train,
        'n_test_draws': n_test,
        'chance_expected_hits': _chance_expected_hits(max_num, main_count),
        'train': _summarize_line_stats(train_stats, max_num, main_count),
        'test': _summarize_line_stats(test_stats, max_num, main_count),
    }


if __name__ == '__main__':
    import json
    result = tsf_forecast('powerball')
    print(json.dumps(result, indent=2, default=str)[:3000])
