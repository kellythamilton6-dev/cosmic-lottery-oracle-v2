"""
One-command calibration health check across every real (game, draw_type)
combination -- run this instead of calling calibration_report() by hand for
whichever combo you happen to remember. Ported from v1 after Florida Lotto
Double Play's Primary preference went unnoticed for a full pass there
(only Main was ever checked).

Also verifies the DB it's about to query isn't stale before reporting
anything -- cosmic_engine.py/match_engine.py/pattern_engine.py all silently
fall back to a local Postgres DB if DATABASE_URL isn't set in the
environment, and that local DB can sit unsynced for weeks (it was over a
month behind production the first time this was checked here).

Usage:
    set -a && source .env && set +a && python3 calibration_sweep.py
"""
import os
import sys
from datetime import datetime, timedelta

from sqlalchemy import create_engine, text

GAME_DRAWTYPES = [
    ("powerball", "main"),
    ("powerball", "doubleplay"),
    ("megamillions", "main"),
]

STALE_DRAW_THRESHOLD_DAYS = 10  # both games draw at least twice a week


def check_data_freshness():
    db_url = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_PUBLIC_URL")
    if not db_url:
        print("WARNING: DATABASE_URL is not set in this shell -- tsf_engine's")
        print("  imports (cosmic_engine/match_engine/pattern_engine) will silently")
        print("  fall back to a local Postgres DB, which may be stale or empty.")
        print("  Run: set -a && source .env && set +a\n")
        return False

    engine = create_engine(db_url)
    tables = {"powerball": "powerball_draws", "megamillions": "megamillions_draws"}
    ok = True
    with engine.connect() as conn:
        for game, table in tables.items():
            row = conn.execute(text(f"SELECT MAX(draw_date), COUNT(*) FROM {table}")).fetchone()
            max_date, count = row[0], row[1]
            age_days = (datetime.now().date() - max_date).days if max_date else None
            stale = age_days is None or age_days > STALE_DRAW_THRESHOLD_DAYS
            ok = ok and not stale
            flag = "STALE" if stale else "fresh"
            print(f"  {game}: most recent draw {max_date} ({age_days}d ago, {count} total draws) -- {flag}")
    print()
    return ok


def main():
    print("=== Data freshness check ===")
    fresh = check_data_freshness()
    if not fresh:
        print("Refusing to run calibration against stale/missing data.")
        print("Fix DATABASE_URL and/or wait for the sync job, then re-run.\n")
        sys.exit(1)

    from tsf_engine import calibration_report

    print("=== Calibration report sweep ===")
    any_flagged = False
    for game, draw_type in GAME_DRAWTYPES:
        r = calibration_report(game, draw_type, months=12)
        if "error" in r:
            print(f"{game}/{draw_type}: ERROR -- {r['error']}")
            continue
        flag = "CHANGES SUGGESTED" if r["any_changes_suggested"] else "clean"
        print(f"{game}/{draw_type}: n_train={r['n_train_draws']} n_test={r['n_test_draws']} -- {flag}")
        if r["any_changes_suggested"]:
            any_flagged = True
            for section in ("primary", "variance"):
                for dim, v in r[section].items():
                    if v["recommendation"] != "no_change":
                        print(f"    [{section}] {dim}: {v['verdict']} -> {v['recommendation']} "
                              f"| train {v['train']} test {v['test']}")

    print()
    if any_flagged:
        print("One or more combinations suggest a change -- review before assuming the")
        print("model config is current.")
    else:
        print("All combinations clean -- no changes suggested anywhere.")


if __name__ == "__main__":
    main()
