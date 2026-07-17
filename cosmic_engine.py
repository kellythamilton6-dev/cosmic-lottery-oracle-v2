import ephem
import math
import random
from datetime import datetime, date
from sqlalchemy import create_engine, text
from collections import Counter, defaultdict

import os
DB_URL = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_PUBLIC_URL") or "postgresql://postgres:Rileyrose69!@localhost:5432/cosmic_lottery_v2"
engine = create_engine(DB_URL)

# ============================================================
# MOON PHASE CALCULATIONS
# ============================================================

def get_moon_phase(target_date):
    d = ephem.Date(target_date.strftime('%Y/%m/%d'))
    nnm = ephem.next_new_moon(d)
    pnm = ephem.previous_new_moon(d)
    cycle_length = nnm - pnm
    position = (d - pnm) / cycle_length
    if position < 0.0625:
        return {'phase': 'New Moon', 'icon': '🌑', 'bias': 'low', 'energy': 0.3}
    elif position < 0.1875:
        return {'phase': 'Waxing Crescent', 'icon': '🌒', 'bias': 'asc', 'energy': 0.5}
    elif position < 0.3125:
        return {'phase': 'First Quarter', 'icon': '🌓', 'bias': 'odd', 'energy': 0.65}
    elif position < 0.4375:
        return {'phase': 'Waxing Gibbous', 'icon': '🌔', 'bias': 'midhigh', 'energy': 0.8}
    elif position < 0.5625:
        return {'phase': 'Full Moon', 'icon': '🌕', 'bias': 'high', 'energy': 1.0}
    elif position < 0.6875:
        return {'phase': 'Waning Gibbous', 'icon': '🌖', 'bias': 'balanced', 'energy': 0.8}
    elif position < 0.8125:
        return {'phase': 'Last Quarter', 'icon': '🌗', 'bias': 'even', 'energy': 0.65}
    else:
        return {'phase': 'Waning Crescent', 'icon': '🌘', 'bias': 'low', 'energy': 0.4}

def get_moon_lucky_numbers(moon_data, max_num=69):
    numbers = set()
    bias = moon_data['bias']
    if bias == 'low':
        for n in range(1, int(max_num * 0.35)):
            numbers.add(n)
    elif bias == 'high':
        for n in range(int(max_num * 0.65), max_num + 1):
            numbers.add(n)
    elif bias == 'odd':
        for n in range(1, max_num + 1, 2):
            numbers.add(n)
    elif bias == 'even':
        for n in range(2, max_num + 1, 2):
            numbers.add(n)
    elif bias == 'asc':
        for n in range(1, int(max_num * 0.6)):
            numbers.add(n)
    elif bias == 'midhigh':
        for n in range(int(max_num * 0.4), max_num + 1):
            numbers.add(n)
    else:
        for n in range(1, max_num + 1):
            numbers.add(n)
    return numbers

# ============================================================
# WESTERN ASTROLOGY
# ============================================================

# Base lucky numbers are the first five terms of each sign's own ruling
# planet's Cheiro numerology cycle (see PLANET_NUMBERS below), keeping this
# list internally consistent with the planet(s) named alongside it. Aquarius
# and Pisces carry dual rulership -- their traditional ruler (Saturn,
# Jupiter) plus their modern-astrology ruler expressed via the Vedic lunar
# nodes (Rahu standing in for Uranus, Ketu for Neptune) -- so their base
# numbers draw from the union of both cycles.
ZODIAC_SIGNS = [
    ('Aries', (3,21), (4,19), [9,18,27,36,45], ['Mars']),
    ('Taurus', (4,20), (5,20), [6,15,24,33,42], ['Venus']),
    ('Gemini', (5,21), (6,20), [5,14,23,32,41], ['Mercury']),
    ('Cancer', (6,21), (7,22), [2,11,20,29,38], ['Moon']),
    ('Leo', (7,23), (8,22), [1,10,19,28,37], ['Sun']),
    ('Virgo', (8,23), (9,22), [5,14,23,32,41], ['Mercury']),
    ('Libra', (9,23), (10,22), [6,15,24,33,42], ['Venus']),
    ('Scorpio', (10,23), (11,21), [9,18,27,36,45], ['Mars']),
    ('Sagittarius', (11,22), (12,21), [3,12,21,30,39], ['Jupiter']),
    ('Capricorn', (12,22), (1,19), [8,17,26,35,44], ['Saturn']),
    ('Aquarius', (1,20), (2,18), [4,8,13,17,22], ['Saturn', 'Rahu']),
    ('Pisces', (2,19), (3,20), [3,7,12,16,21], ['Jupiter', 'Ketu']),
]

PLANET_NUMBERS = {
    'Sun': [1,10,19,28,37,46,55,64],
    'Moon': [2,11,20,29,38,47,56,65],
    'Mars': [9,18,27,36,45,54,63],
    'Mercury': [5,14,23,32,41,50,59,68],
    'Jupiter': [3,12,21,30,39,48,57,66],
    'Venus': [6,15,24,33,42,51,60,69],
    'Saturn': [8,17,26,35,44,53,62],
    'Rahu': [4,13,22,31,40,49,58,67],
    'Ketu': [7,16,25,34,43,52,61,70],
}

def get_sun_sign(birth_date):
    m, d = birth_date.month, birth_date.day
    for sign, start, end, luckies, planets in ZODIAC_SIGNS:
        sm, sd = start
        em, ed = end
        if (m == sm and d >= sd) or (m == em and d <= ed):
            return {'sign': sign, 'lucky': luckies, 'planets': planets}
    return {'sign': 'Capricorn', 'lucky': [8,17,26,35,44], 'planets': ['Saturn']}

def get_astro_lucky_numbers(sun_sign_data, max_num=69):
    numbers = set()
    base = sun_sign_data['lucky']
    planets = sun_sign_data['planets']
    for n in base:
        for mult in range(1, 5):
            if n * mult <= max_num:
                numbers.add(n * mult)
    for planet in planets:
        for n in PLANET_NUMBERS.get(planet, []):
            if n <= max_num:
                numbers.add(n)
    return numbers

# ============================================================
# VEDIC ASTROLOGY
# ============================================================

NAKSHATRAS = [
    ('Ashwini', 'Ketu', [1,10,19]),
    ('Bharani', 'Venus', [2,11,20]),
    ('Krittika', 'Sun', [3,12,21]),
    ('Rohini', 'Moon', [4,13,22]),
    ('Mrigashira', 'Mars', [5,14,23]),
    ('Ardra', 'Rahu', [6,15,24]),
    ('Punarvasu', 'Jupiter', [7,16,25]),
    ('Pushya', 'Saturn', [8,17,26]),
    ('Ashlesha', 'Mercury', [9,18,27]),
    ('Magha', 'Ketu', [1,10,28]),
    ('Purva Phalguni', 'Venus', [2,11,29]),
    ('Uttara Phalguni', 'Sun', [3,12,30]),
    ('Hasta', 'Moon', [4,13,31]),
    ('Chitra', 'Mars', [5,14,32]),
    ('Swati', 'Rahu', [6,15,33]),
    ('Vishakha', 'Jupiter', [7,16,34]),
    ('Anuradha', 'Saturn', [8,17,35]),
    ('Jyeshtha', 'Mercury', [9,18,36]),
    ('Mula', 'Ketu', [1,19,37]),
    ('Purva Ashadha', 'Venus', [2,20,38]),
    ('Uttara Ashadha', 'Sun', [3,21,39]),
    ('Shravana', 'Moon', [4,22,40]),
    ('Dhanishta', 'Mars', [5,23,41]),
    ('Shatabhisha', 'Rahu', [6,24,42]),
    ('Purva Bhadra', 'Jupiter', [7,25,43]),
    ('Uttara Bhadra', 'Saturn', [8,26,44]),
    ('Revati', 'Mercury', [9,27,45]),
]

def get_nakshatra(target_date):
    moon = ephem.Moon(target_date.strftime('%Y/%m/%d'))
    moon_lon = float(moon.ra) * 180 / math.pi
    nak_index = int((moon_lon % 360) / (360/27))
    nak_index = min(nak_index, 26)
    return NAKSHATRAS[nak_index]

def get_vedic_lucky_numbers(nakshatra_data, max_num=69):
    numbers = set()
    base = nakshatra_data[2]
    planet = nakshatra_data[1]
    for n in base:
        for mult in range(1, 6):
            if n * mult <= max_num:
                numbers.add(n * mult)
    for n in PLANET_NUMBERS.get(planet, []):
        if n <= max_num:
            numbers.add(n)
    return numbers

# ============================================================
# NUMEROLOGY
# ============================================================

PYTHAGOREAN = {
    'a':1,'b':2,'c':3,'d':4,'e':5,'f':6,'g':7,'h':8,'i':9,
    'j':1,'k':2,'l':3,'m':4,'n':5,'o':6,'p':7,'q':8,'r':9,
    's':1,'t':2,'u':3,'v':4,'w':5,'x':6,'y':7,'z':8
}

def reduce_number(n):
    while n > 9 and n not in [11, 22, 33]:
        n = sum(int(d) for d in str(n))
    return n

def get_life_path(birth_date):
    total = sum(int(d) for d in birth_date.strftime('%m%d%Y'))
    return reduce_number(total)

def get_name_number(name):
    if not name:
        return 0
    total = sum(PYTHAGOREAN.get(c.lower(), 0) for c in name if c.isalpha())
    return reduce_number(total)

def get_draw_date_number(target_date):
    total = sum(int(d) for d in target_date.strftime('%m%d%Y'))
    return reduce_number(total)

def get_numerology_lucky_numbers(life_path, name_num, date_num, max_num=69):
    numbers = set()
    for base in [life_path, name_num, date_num]:
        if base == 0:
            continue
        for mult in range(1, max_num // base + 2):
            if base * mult <= max_num:
                numbers.add(base * mult)
        for offset in [-1, 0, 1]:
            n = base + offset
            if 1 <= n <= max_num:
                numbers.add(n)
    return numbers

# ============================================================
# HISTORICAL PATTERN ANALYSIS
# ============================================================

def get_historical_hot_numbers(game='powerball', limit=50):
    table = 'powerball_draws' if game == 'powerball' else 'megamillions_draws'
    bonus_col = 'powerball' if game == 'powerball' else 'megaball'
    try:
        with engine.connect() as conn:
            result = conn.execute(text(f"""
                SELECT n1, n2, n3, n4, n5 FROM {table}
                ORDER BY draw_date DESC LIMIT {limit}
            """))
            rows = result.fetchall()
        all_nums = []
        for row in rows:
            all_nums.extend([row[0], row[1], row[2], row[3], row[4]])
        freq = Counter(all_nums)
        hot = set(n for n, c in freq.most_common(20))
        cold = set(n for n, c in freq.most_common()[:-21:-1])
        return {'hot': hot, 'cold': cold, 'frequency': freq}
    except Exception as e:
        print(f"DB warning: {e}")
        return {'hot': set(), 'cold': set(), 'frequency': {}}

def get_moon_phase_patterns(game='powerball'):
    table = 'powerball_draws' if game == 'powerball' else 'megamillions_draws'
    try:
        with engine.connect() as conn:
            result = conn.execute(text(f"""
                SELECT draw_date, n1, n2, n3, n4, n5 FROM {table}
                ORDER BY draw_date DESC LIMIT 200
            """))
            rows = result.fetchall()
        phase_numbers = defaultdict(list)
        for row in rows:
            try:
                phase = get_moon_phase(row[0])['phase']
                phase_numbers[phase].extend([row[1], row[2], row[3], row[4], row[5]])
            except:
                continue
        return {phase: Counter(nums).most_common(10) for phase, nums in phase_numbers.items()}
    except Exception as e:
        print(f"DB warning: {e}")
        return {}

# ============================================================
# MAIN PREDICTION ENGINE
# ============================================================

def generate_predictions(
    birth_date,
    target_date=None,
    name='',
    game='powerball',
    w_moon=0.30,
    w_astro=0.25,
    w_vedic=0.25,
    w_num=0.20
):
    if target_date is None:
        target_date = date.today()

    configs = {
        'powerball': {'main': 5, 'max': 69, 'bonus_max': 26},
        'megamillions': {'main': 5, 'max': 70, 'bonus_max': 25},
    }
    cfg = configs.get(game, configs['powerball'])
    max_num = cfg['max']

    moon = get_moon_phase(target_date)
    sun_sign = get_sun_sign(birth_date)
    nakshatra = get_nakshatra(target_date)
    life_path = get_life_path(birth_date)
    name_num = get_name_number(name)
    date_num = get_draw_date_number(target_date)
    history = get_historical_hot_numbers(game)

    pool = {n: 10.0 for n in range(1, max_num + 1)}

    moon_nums = get_moon_lucky_numbers(moon, max_num)
    for n in moon_nums:
        pool[n] = pool.get(n, 50) + (w_moon * 500)

    astro_nums = get_astro_lucky_numbers(sun_sign, max_num)
    for n in astro_nums:
        pool[n] = pool.get(n, 50) + (w_astro * 500)

    vedic_nums = get_vedic_lucky_numbers(nakshatra, max_num)
    for n in vedic_nums:
        pool[n] = pool.get(n, 50) + (w_vedic * 500)

    num_nums = get_numerology_lucky_numbers(life_path, name_num, date_num, max_num)
    for n in num_nums:
        pool[n] = pool.get(n, 50) + (w_num * 500)

    for n in history['hot']:
        if n in pool:
            pool[n] *= 1.3

    for n in history['cold']:
        if n in pool:
            pool[n] *= 1.1

    def weighted_pick(count, exclude=set()):
        available = {n: w for n, w in pool.items() if n not in exclude}
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

    primary = weighted_pick(cfg['main'])
    alt_a = weighted_pick(cfg['main'])
    alt_b = weighted_pick(cfg['main'])

    bonus_primary = bonus_alt_a = bonus_alt_b = None
    if cfg['bonus_max'] > 0:
        bonus_pool = {n: 50.0 for n in range(1, cfg['bonus_max'] + 1)}
        for n in get_numerology_lucky_numbers(life_path, name_num, date_num, cfg['bonus_max']):
            bonus_pool[n] = bonus_pool.get(n, 50) + 50
        def pick_bonus():
            total = sum(bonus_pool.values())
            r = random.uniform(0, total)
            cum = 0
            for n, w in sorted(bonus_pool.items()):
                cum += w
                if r <= cum:
                    return n
            return 1
        bonus_primary = pick_bonus()
        bonus_alt_a = pick_bonus()
        bonus_alt_b = pick_bonus()

    return {
        'primary': primary,
        'alt_a': alt_a,
        'alt_b': alt_b,
        'bonus_primary': bonus_primary,
        'bonus_alt_a': bonus_alt_a,
        'bonus_alt_b': bonus_alt_b,
        'moon': moon,
        'sun_sign': sun_sign,
        'nakshatra': nakshatra,
        'life_path': life_path,
        'name_number': name_num,
        'date_number': date_num,
        'game': game,
        'draw_date': target_date.isoformat(),
        'birth_date': birth_date.isoformat(),
    }

# ============================================================
# TEST
# ============================================================

if __name__ == '__main__':
    print("\n✦ COSMIC LOTTERY ORACLE ✦")
    print("Running prediction engine test...")
    result = generate_predictions(
        birth_date=date(1985, 6, 14),
        target_date=date.today(),
        name='Lucky',
        game='powerball'
    )
    print(f"Draw date:    {result['draw_date']}")
    print(f"Moon phase:   {result['moon']['icon']} {result['moon']['phase']}")
    print(f"Sun sign:     {result['sun_sign']['sign']}")
    print(f"Nakshatra:    {result['nakshatra'][0]}")
    print(f"Life path:    {result['life_path']}")
    print(f"\nPrimary:     {result['primary']} + PB: {result['bonus_primary']}")
    print(f"Alt A:       {result['alt_a']} + PB: {result['bonus_alt_a']}")
    print(f"Alt B:       {result['alt_b']} + PB: {result['bonus_alt_b']}")
    print("\n✦ Engine working successfully! ✦")