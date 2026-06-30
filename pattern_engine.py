import math
from collections import Counter, defaultdict
from datetime import datetime
from sqlalchemy import create_engine, text
import random

import os
DB_URL = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_PUBLIC_URL") or "postgresql://postgres:Rileyrose69!@localhost:5432/cosmic_lottery_v2"
engine = create_engine(DB_URL)

# ============================================================
# DATA LOADER
# ============================================================

def load_draws(game='powerball', limit=None):
    if game == 'megamillions':
        table = 'megamillions_draws'
        bonus_col = 'megaball'
    else:
        table = 'powerball_draws'
        bonus_col = 'powerball'
    limit_clause = f'LIMIT {limit}' if limit else ''
    with engine.connect() as conn:
        if bonus_col:
            result = conn.execute(text(f"""
                SELECT draw_date, n1, n2, n3, n4, n5, {bonus_col}
                FROM {table}
                ORDER BY draw_date DESC {limit_clause}
            """))
            rows = result.fetchall()
            return [{'date': str(r[0]), 'numbers': sorted([r[1],r[2],r[3],r[4],r[5]]), 'bonus': r[6]} for r in rows]
        else:
            result = conn.execute(text(f"""
                SELECT draw_date, n1, n2, n3, n4, n5
                FROM {table}
                ORDER BY draw_date DESC {limit_clause}
            """))
            rows = result.fetchall()
            return [{'date': str(r[0]), 'numbers': sorted([r[1],r[2],r[3],r[4],r[5]]), 'bonus': None} for r in rows]

# ============================================================
# 1. FREQUENCY ANALYSIS
# ============================================================

def frequency_analysis(draws, windows=[10,50,100,200]):
    all_nums = []
    for d in draws:
        all_nums.extend(d['numbers'])
    total_freq = Counter(all_nums)

    window_freqs = {}
    for w in windows:
        recent = draws[:w] if len(draws) >= w else draws
        nums = []
        for d in recent:
            nums.extend(d['numbers'])
        window_freqs[w] = Counter(nums)

    return {
        'total': dict(total_freq),
        'windows': {str(w): dict(f) for w, f in window_freqs.items()},
        'hot': [n for n, c in total_freq.most_common(15)],
        'cold': [n for n, c in total_freq.most_common()[:-16:-1]],
    }

# ============================================================
# 2. GAP ANALYSIS
# ============================================================

def gap_analysis(draws, max_num=69):
    last_seen = {}
    gaps = defaultdict(list)

    for i, draw in enumerate(draws):
        for n in draw['numbers']:
            if n in last_seen:
                gaps[n].append(i - last_seen[n])
            last_seen[n] = i

    result = {}
    for n in range(1, max_num + 1):
        g = gaps.get(n, [])
        current_gap = draws.index(next((d for d in draws if n in d['numbers']), draws[-1]))
        avg_gap = sum(g) / len(g) if g else 0
        result[n] = {
            'current_gap': current_gap,
            'avg_gap': round(avg_gap, 1),
            'max_gap': max(g) if g else 0,
            'overdue': current_gap > avg_gap * 1.5 if avg_gap > 0 else False,
            'overdue_score': round((current_gap / avg_gap) if avg_gap > 0 else 0, 2)
        }
    return result

# ============================================================
# 3. PAIR & TRIPLET FREQUENCY
# ============================================================

def pair_analysis(draws, top_n=20):
    draws = draws[:200]
    pairs = Counter()
    triplets = Counter()

    for draw in draws:
        nums = sorted(draw['numbers'])
        for i in range(len(nums)):
            for j in range(i+1, len(nums)):
                pairs[(nums[i], nums[j])] += 1
            for j in range(i+1, len(nums)):
                for k in range(j+1, len(nums)):
                    triplets[(nums[i], nums[j], nums[k])] += 1

    return {
        'top_pairs': [{'numbers': list(p), 'count': c} for p, c in pairs.most_common(top_n)],
        'top_triplets': [{'numbers': list(t), 'count': c} for t, c in triplets.most_common(top_n)],
    }

# ============================================================
# 4. SUM & BALANCE ANALYSIS
# ============================================================

def sum_balance_analysis(draws):
    sums = [sum(d['numbers']) for d in draws]
    odd_counts = [len([n for n in d['numbers'] if n % 2 != 0]) for d in draws]
    high_counts = [len([n for n in d['numbers'] if n > 35]) for d in draws]

    avg_sum = sum(sums) / len(sums)
    std_sum = math.sqrt(sum((s - avg_sum)**2 for s in sums) / len(sums))
    avg_odd = sum(odd_counts) / len(odd_counts)
    avg_high = sum(high_counts) / len(high_counts)

    sum_ranges = Counter()
    for s in sums:
        bucket = (s // 20) * 20
        sum_ranges[f'{bucket}-{bucket+19}'] += 1

    return {
        'avg_sum': round(avg_sum, 1),
        'std_sum': round(std_sum, 1),
        'optimal_sum_range': [round(avg_sum - std_sum), round(avg_sum + std_sum)],
        'avg_odd_count': round(avg_odd, 1),
        'avg_high_count': round(avg_high, 1),
        'sum_distribution': dict(sum_ranges.most_common(10)),
        'recommended_odd_even': f'{round(avg_odd)}/{5-round(avg_odd)}',
        'recommended_high_low': f'{round(avg_high)}/{5-round(avg_high)}',
    }

# ============================================================
# 5. CONSECUTIVE NUMBER ANALYSIS
# ============================================================

def consecutive_analysis(draws):
    consec_counts = []
    for draw in draws:
        nums = sorted(draw['numbers'])
        consec = sum(1 for i in range(len(nums)-1) if nums[i+1] - nums[i] == 1)
        consec_counts.append(consec)

    avg_consec = sum(consec_counts) / len(consec_counts)
    consec_pairs = Counter()
    for draw in draws:
        nums = sorted(draw['numbers'])
        for i in range(len(nums)-1):
            if nums[i+1] - nums[i] == 1:
                consec_pairs[(nums[i], nums[i+1])] += 1

    return {
        'avg_consecutive_pairs': round(avg_consec, 2),
        'draws_with_consecutive': sum(1 for c in consec_counts if c > 0),
        'pct_with_consecutive': round(sum(1 for c in consec_counts if c > 0) / len(draws) * 100, 1),
        'top_consecutive_pairs': [{'pair': list(p), 'count': c} for p, c in consec_pairs.most_common(10)],
    }

# ============================================================
# 6. POSITIONAL ANALYSIS
# ============================================================

def positional_analysis(draws, positions=5):
    pos_counters = [Counter() for _ in range(positions)]
    for draw in draws:
        nums = sorted(draw['numbers'])
        for i, n in enumerate(nums[:positions]):
            pos_counters[i][n] += 1

    return {
        f'position_{i+1}': {
            'top_5': [n for n, c in counter.most_common(5)],
            'distribution': dict(counter.most_common(10))
        }
        for i, counter in enumerate(pos_counters)
    }

# ============================================================
# 7. MARKOV CHAIN ANALYSIS
# ============================================================

def markov_analysis(draws, max_num=69):
    transitions = defaultdict(Counter)
    for i in range(len(draws) - 1):
        current_nums = draws[i]['numbers']
        next_nums = draws[i+1]['numbers']
        for cn in current_nums:
            for nn in next_nums:
                transitions[cn][nn] += 1

    last_draw_nums = draws[0]['numbers'] if draws else []
    next_prob = Counter()
    for n in last_draw_nums:
        for next_n, count in transitions[n].items():
            next_prob[next_n] += count

    return {
        'last_draw': last_draw_nums,
        'most_likely_next': [n for n, c in next_prob.most_common(20)],
        'transition_strength': {str(n): dict(transitions[n].most_common(5)) for n in last_draw_nums},
    }

# ============================================================
# 8. DELTA SYSTEM
# ============================================================

def delta_analysis(draws):
    all_deltas = []
    for draw in draws:
        nums = sorted(draw['numbers'])
        deltas = [nums[0]] + [nums[i+1] - nums[i] for i in range(len(nums)-1)]
        all_deltas.append(deltas)

    if not all_deltas:
        return {}

    avg_deltas = [
        round(sum(d[i] for d in all_deltas if i < len(d)) / len(all_deltas), 1)
        for i in range(5)
    ]

    delta_freq = Counter()
    for deltas in all_deltas:
        for d in deltas:
            delta_freq[d] += 1

    return {
        'avg_deltas': avg_deltas,
        'most_common_deltas': [d for d, c in delta_freq.most_common(10)],
        'typical_pattern': avg_deltas,
        'description': 'Delta values show typical spacing between drawn numbers'
    }

# ============================================================
# 9. MOON PHASE CORRELATION
# ============================================================

def moon_phase_correlation(draws):
    try:
        import ephem
        import math as m
        phase_nums = defaultdict(list)
        phases = ['New Moon','Waxing Crescent','First Quarter','Waxing Gibbous',
                  'Full Moon','Waning Gibbous','Last Quarter','Waning Crescent']

        for draw in draws:
            try:
                d = ephem.Date(draw['date'].replace('-','/'))
                nnm = ephem.next_new_moon(d)
                pnm = ephem.previous_new_moon(d)
                pos = (d - pnm) / (nnm - pnm)
                idx = min(int(pos * 8), 7)
                phase = phases[idx]
                phase_nums[phase].extend(draw['numbers'])
            except:
                continue

        result = {}
        for phase in phases:
            nums = phase_nums.get(phase, [])
            if nums:
                freq = Counter(nums)
                result[phase] = {
                    'top_numbers': [n for n, c in freq.most_common(10)],
                    'avg_number': round(sum(nums)/len(nums), 1),
                    'sample_size': len(nums) // 5
                }
        return result
    except Exception as e:
        return {'error': str(e)}

# ============================================================
# 10. CYCLICAL / DAY OF WEEK ANALYSIS
# ============================================================

def cyclical_analysis(draws):
    day_nums = defaultdict(list)
    month_nums = defaultdict(list)
    days = ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday']

    for draw in draws:
        try:
            dt = datetime.strptime(draw['date'], '%Y-%m-%d')
            day_nums[days[dt.weekday()]].extend(draw['numbers'])
            month_nums[dt.month].extend(draw['numbers'])
        except:
            continue

    day_patterns = {}
    for day, nums in day_nums.items():
        if nums:
            freq = Counter(nums)
            day_patterns[day] = {
                'top_numbers': [n for n, c in freq.most_common(8)],
                'avg': round(sum(nums)/len(nums), 1),
                'sample_size': len(nums) // 5
            }

    month_names = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']
    month_patterns = {}
    for month, nums in month_nums.items():
        if nums:
            freq = Counter(nums)
            month_patterns[month_names[month-1]] = {
                'top_numbers': [n for n, c in freq.most_common(8)],
                'avg': round(sum(nums)/len(nums), 1)
            }

    return {
        'by_day': day_patterns,
        'by_month': month_patterns,
    }

# ============================================================
# 11. CLUSTER ANALYSIS
# ============================================================

def cluster_analysis(draws, max_num=69):
    co_occurrence = defaultdict(Counter)
    for draw in draws[:200]:
        nums = draw['numbers']
        for i in range(len(nums)):
            for j in range(len(nums)):
                if i != j:
                    co_occurrence[nums[i]][nums[j]] += 1

    clusters = []
    used = set()
    for n in range(1, max_num + 1):
        if n not in used and n in co_occurrence:
            cluster = [n]
            used.add(n)
            for companion, count in co_occurrence[n].most_common(2):
                if companion not in used:
                    cluster.append(companion)
                    used.add(companion)
            if len(cluster) > 1:
                clusters.append(sorted(cluster))

    return {
        'clusters': clusters[:10],
        'strongest_companions': {
            str(n): [x for x, c in co_occurrence[n].most_common(3)]
            for n in range(1, min(15, max_num+1))
        }
    }

# ============================================================
# MASTER PREDICTION FUNCTION
# ============================================================

def pattern_predict(game='powerball', confidence_weight=True):
    draws = load_draws(game)
    if not draws:
        return {'error': 'No historical data found'}

    max_num = 69 if game == 'powerball' else 70
    main_count = 5
    bonus_max = 26 if game == 'powerball' else 25

    freq = frequency_analysis(draws)
    gaps = gap_analysis(draws, max_num)
    pairs = pair_analysis(draws)
    balance = sum_balance_analysis(draws)
    markov = markov_analysis(draws, max_num)
    delta = delta_analysis(draws)
    moon = moon_phase_correlation(draws)
    cycles = cyclical_analysis(draws)
    clusters = cluster_analysis(draws, max_num)
    consecutive = consecutive_analysis(draws)
    positional = positional_analysis(draws)

    # Build confidence scores for every number
    scores = {n: 0.0 for n in range(1, max_num + 1)}

    # Frequency score (30%)
    total_f = sum(freq['total'].values()) or 1
    for n in range(1, max_num + 1):
        scores[n] += (freq['total'].get(n, 0) / total_f) * 30

    # Recent window bonus (20%) — last 50 draws
    recent_f = freq['windows'].get('50', {})
    recent_total = sum(recent_f.values()) or 1
    for n in range(1, max_num + 1):
        scores[n] += (recent_f.get(n, 0) / recent_total) * 20

    # Overdue bonus (20%) — numbers past their average gap
    max_overdue = max((gaps[n]['overdue_score'] for n in range(1, max_num+1)), default=1)
    for n in range(1, max_num + 1):
        if gaps[n]['overdue']:
            scores[n] += (gaps[n]['overdue_score'] / max(max_overdue, 1)) * 20

    # Markov bonus (15%) — likely to follow last draw
    markov_nums = markov.get('most_likely_next', [])
    for i, n in enumerate(markov_nums):
        scores[n] = scores.get(n, 0) + (15 - i * 0.5)

    # Moon phase bonus (10%)
    from cosmic_engine import get_moon_phase
    from datetime import date
    current_phase = get_moon_phase(date.today())['phase']
    moon_top = moon.get(current_phase, {}).get('top_numbers', [])
    for i, n in enumerate(moon_top):
        if n <= max_num:
            scores[n] = scores.get(n, 0) + (10 - i * 0.8)

    # Cluster bonus (5%)
    for cluster in clusters['clusters']:
        for n in cluster:
            if n <= max_num:
                scores[n] = scores.get(n, 0) + 2

    # Normalize to 0-100
    max_score = max(scores.values()) or 1
    confidence = {n: round((scores[n] / max_score) * 100, 1) for n in range(1, max_num + 1)}

    # Generate prediction sets using confidence scores
    def weighted_pick(count, exclude=set()):
        available = {n: scores[n] for n in scores if n not in exclude and n >= 1 and n <= max_num}
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

    primary = weighted_pick(main_count)
    alt_a = weighted_pick(main_count)
    alt_b = weighted_pick(main_count)

    # Verify sum is in optimal range
    opt_low, opt_high = balance['optimal_sum_range']

    # Bonus ball
    bonus_scores = {n: 50.0 for n in range(1, bonus_max + 1)}
    bonus_draws = [d['bonus'] for d in draws]
    bonus_freq = Counter(bonus_draws)
    for n in range(1, bonus_max + 1):
        bonus_scores[n] += bonus_freq.get(n, 0) * 3
    def pick_bonus():
        if bonus_max == 0:
            return None
        total = sum(bonus_scores.values())
        r = random.uniform(0, total)
        cum = 0
        for n, w in sorted(bonus_scores.items()):
            cum += w
            if r <= cum:
                return n
        return 1

    top_confidence = sorted(confidence.items(), key=lambda x: x[1], reverse=True)

    return {
        'game': game,
        'total_draws': len(draws),
        'primary': primary,
        'alt_a': alt_a,
        'alt_b': alt_b,
        'bonus_primary': pick_bonus(),
        'bonus_alt_a': pick_bonus(),
        'bonus_alt_b': pick_bonus(),
        'confidence_scores': confidence,
        'top_20_by_confidence': [{'number': n, 'score': s} for n, s in top_confidence[:20]],
        'balance': balance,
        'frequency': freq,
        'gaps': {str(k): v for k, v in gaps.items()},
        'markov': markov,
        'delta': delta,
        'moon_correlation': moon,
        'cycles': cycles,
        'clusters': clusters,
        'consecutive': consecutive,
        'positional': positional,
        'pairs': pairs,
        'current_moon_phase': current_phase,
        'sum_check': {
            'primary_sum': sum(primary),
            'in_optimal_range': opt_low <= sum(primary) <= opt_high,
            'optimal_range': [opt_low, opt_high]
        }
    }

if __name__ == '__main__':
    print("Running pattern analysis...")
    result = pattern_predict('powerball')
    print(f"Total draws analyzed: {result['total_draws']}")
    print(f"Primary prediction: {result['primary']} + {result['bonus_primary']}")
    print(f"Sum: {result['sum_check']['primary_sum']} (optimal: {result['sum_check']['optimal_range']})")
    print(f"Top 10 by confidence: {[x['number'] for x in result['top_20_by_confidence'][:10]]}")
    print("Pattern analysis complete!")