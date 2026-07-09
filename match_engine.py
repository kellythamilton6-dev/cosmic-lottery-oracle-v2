"""Structural pattern-matching engine for the Pattern Match tab.

Ranks historical draws by "shape" similarity to a target draw (sum, odd/even
split, low/high split, spread, consecutive-number count, decade histogram)
rather than by which exact numbers were drawn, and surfaces the drawing
immediately before/after each match plus positional and follow-up-number
averages across the matches.
"""

import os
from sqlalchemy import create_engine, text

DB_URL = (
    os.environ.get("DATABASE_URL")
    or os.environ.get("DATABASE_PUBLIC_URL")
    or "postgresql://postgres:Rileyrose69!@localhost:5432/cosmic_lottery_v2"
)
engine = create_engine(DB_URL)

GAME_CONFIG = {
    "powerball": {"table": "powerball_draws", "bonus_col": "powerball", "max_num": 69, "bonus_max": 26},
    "megamillions": {"table": "megamillions_draws", "bonus_col": "megaball", "max_num": 70, "bonus_max": 25},
}

MAX_CONSECUTIVE_PAIRS = 4
WEIGHTS = {"sum": 0.25, "odd_even": 0.15, "low_high": 0.15, "spread": 0.15, "consecutive": 0.10, "decade": 0.20}


def _config(game):
    return GAME_CONFIG.get(game, GAME_CONFIG["powerball"])


def load_all_draws(game):
    """All draws for `game`, ascending by date, each tagged with a sequential
    id so `id - 1` / `id + 1` is literally the previous/next drawing."""
    cfg = _config(game)
    with engine.connect() as conn:
        result = conn.execute(text(f"""
            SELECT draw_date, n1, n2, n3, n4, n5, {cfg['bonus_col']}
            FROM {cfg['table']}
            ORDER BY draw_date ASC
        """))
        rows = result.fetchall()
    return [
        {"id": i, "date": str(r[0]), "numbers": sorted([r[1], r[2], r[3], r[4], r[5]]), "bonus": r[6]}
        for i, r in enumerate(rows)
    ]


def _decade_bins(max_num):
    bins, lo = [], 1
    while lo <= max_num:
        hi = min(lo + 9, max_num)
        bins.append((lo, hi))
        lo += 10
    return bins


def compute_features(numbers, max_num):
    nums = sorted(numbers)
    split = (max_num + 1) // 2  # e.g. 35 for max_num 69, splitting 1-35 / 36-69
    odd_count = sum(1 for n in nums if n % 2 == 1)
    low_count = sum(1 for n in nums if n <= split)
    spread = nums[-1] - nums[0]
    consecutive = sum(1 for a, b in zip(nums, nums[1:]) if b - a == 1)
    histogram = [sum(1 for n in nums if lo <= n <= hi) for lo, hi in _decade_bins(max_num)]
    return {
        "sum": sum(nums),
        "odd_count": odd_count,
        "even_count": 5 - odd_count,
        "low_count": low_count,
        "high_count": 5 - low_count,
        "spread": spread,
        "consecutive_pairs": consecutive,
        "decade_histogram": histogram,
    }


def similarity_score(a, b, max_num):
    max_spread = max_num - 1
    diffs = {
        "sum": abs(a["sum"] - b["sum"]) / (max_num * 5 * 0.65),
        "odd_even": abs(a["odd_count"] - b["odd_count"]) / 5,
        "low_high": abs(a["low_count"] - b["low_count"]) / 5,
        "spread": abs(a["spread"] - b["spread"]) / max_spread,
        "consecutive": abs(a["consecutive_pairs"] - b["consecutive_pairs"]) / MAX_CONSECUTIVE_PAIRS,
        "decade": sum(abs(x - y) for x, y in zip(a["decade_histogram"], b["decade_histogram"])) / 10,
    }
    weighted = sum(min(diffs[k], 1.0) * WEIGHTS[k] for k in WEIGHTS)
    return round(max(0.0, 1.0 - weighted) * 100, 1)


def _neighbor(draws, target_id, delta):
    idx = target_id + delta
    return draws[idx] if 0 <= idx < len(draws) else None


def _positional_averages(matches):
    positions = []
    for i in range(5):
        vals = [m["numbers"][i] for m in matches]
        positions.append({
            "avg": round(sum(vals) / len(vals), 1) if vals else None,
            "min": min(vals) if vals else None,
            "max": max(vals) if vals else None,
        })
    bonus_vals = [m["bonus"] for m in matches if m["bonus"] is not None]
    bonus = {
        "avg": round(sum(bonus_vals) / len(bonus_vals), 1) if bonus_vals else None,
        "min": min(bonus_vals) if bonus_vals else None,
        "max": max(bonus_vals) if bonus_vals else None,
    }
    return positions, bonus


def _followup_frequency(matches):
    white_counts, bonus_counts = {}, {}
    for m in matches:
        nd = m.get("next_drawing")
        if not nd:
            continue
        for n in nd["numbers"]:
            white_counts[n] = white_counts.get(n, 0) + 1
        bonus_counts[nd["bonus"]] = bonus_counts.get(nd["bonus"], 0) + 1
    white_sorted = sorted(white_counts.items(), key=lambda x: (-x[1], x[0]))[:15]
    bonus_sorted = sorted(bonus_counts.items(), key=lambda x: (-x[1], x[0]))[:10]
    return white_sorted, bonus_sorted


def pattern_match(game, draw_id=None, custom_numbers=None, custom_bonus=None, limit=10):
    """Rank historical draws by structural similarity to a target draw.

    Target is either a stored draw (`draw_id`, defaults to the most recent)
    or ad-hoc `custom_numbers` (+ optional `custom_bonus`) that aren't part
    of the historical sequence.
    """
    cfg = _config(game)
    max_num = cfg["max_num"]
    draws = load_all_draws(game)
    if not draws:
        return None

    if custom_numbers is not None:
        target = {"id": -1, "date": "Custom numbers", "numbers": sorted(custom_numbers), "bonus": custom_bonus}
    elif draw_id is not None:
        target = next((d for d in draws if d["id"] == draw_id), None)
        if target is None:
            return None
    else:
        target = draws[-1]

    target_features = compute_features(target["numbers"], max_num)

    scored = []
    for d in draws:
        if d["id"] == target["id"]:
            continue
        f = compute_features(d["numbers"], max_num)
        scored.append({**d, "score": similarity_score(target_features, f, max_num), "features": f})
    scored.sort(key=lambda x: x["score"], reverse=True)
    top = scored[:limit]

    for m in top:
        m["previous_drawing"] = _neighbor(draws, m["id"], -1)
        m["next_drawing"] = _neighbor(draws, m["id"], 1)

    positional_averages, bonus_positional_average = _positional_averages(top)
    followup_white_frequency, followup_bonus_frequency = _followup_frequency(top)

    return {
        "game": game,
        "max_num": max_num,
        "bonus_max": cfg["bonus_max"],
        "target": {**target, "features": target_features},
        "pool_size": len(draws) - (0 if custom_numbers is not None else 1),
        "matches": top,
        "positional_averages": positional_averages,
        "bonus_positional_average": bonus_positional_average,
        "followup_white_frequency": followup_white_frequency,
        "followup_bonus_frequency": followup_bonus_frequency,
    }


def recent_draws_for_picker(game, limit=100):
    draws = load_all_draws(game)
    recent = draws[-limit:][::-1]
    return [{"id": d["id"], "date": d["date"], "numbers": d["numbers"], "bonus": d["bonus"]} for d in recent]
