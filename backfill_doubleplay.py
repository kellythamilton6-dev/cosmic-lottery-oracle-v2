"""Add Powerball Double Play draws to powerball_draws.

Double Play shares the same draw_date as that night's main Powerball
drawing, so this first widens the unique constraint from (draw_date) to
(draw_date, game) to let both coexist, then backfills historical Double
Play numbers (2021-08-23 through 2026-07-06) from a verified source: the
official Florida Lottery winning-numbers-history PDF, already extracted
and cross-checked while building a related tool.
(source: ../lottolens/backend/data/fl_powerball_history.json)

Ongoing Double Play draws going forward are handled separately by
sync_game() in api.py, which reads the double_play_winning_numbers field
NY Open Data already includes inline on the main Powerball dataset.

Usage:
    DATABASE_URL=... python3 backfill_doubleplay.py            # dry run
    DATABASE_URL=... python3 backfill_doubleplay.py --apply     # actually writes
"""

import json
import os
import sys
from sqlalchemy import create_engine, text

DB_URL = (
    os.environ.get("DATABASE_URL")
    or os.environ.get("DATABASE_PUBLIC_URL")
    or "postgresql://postgres:Rileyrose69!@localhost:5432/cosmic_lottery_v2"
)
engine = create_engine(DB_URL)
APPLY = "--apply" in sys.argv

SOURCE_PATH = os.path.expanduser(
    "~/lottolens/backend/data/fl_powerball_history.json"
)


def main():
    with open(SOURCE_PATH) as f:
        records = json.load(f)
    dp_rows = [r for r in records if r["game"] == "powerball_doubleplay"]
    print(f"Loaded {len(dp_rows)} Double Play records from {SOURCE_PATH}")
    print(f"Date range: {dp_rows[0]['draw_date']} to {dp_rows[-1]['draw_date']}")

    with engine.connect() as conn:
        existing_dp = conn.execute(text(
            "SELECT count(*) FROM powerball_draws WHERE game = 'powerball_doubleplay'"
        )).scalar()
        print(f"powerball_draws currently has {existing_dp} Double Play rows")

        mode = "APPLYING CHANGES" if APPLY else "DRY RUN (pass --apply to write)"
        print(f"\n=== {mode} ===")
        if not APPLY:
            return

        existing_constraint = conn.execute(text("""
            SELECT constraint_name FROM information_schema.table_constraints
            WHERE table_name = 'powerball_draws' AND constraint_type = 'UNIQUE'
        """)).fetchall()
        for row in existing_constraint:
            conn.execute(text(f"ALTER TABLE powerball_draws DROP CONSTRAINT {row[0]}"))
        conn.execute(text(
            "ALTER TABLE powerball_draws ADD CONSTRAINT powerball_draws_date_game_unique UNIQUE (draw_date, game)"
        ))
        print("Unique constraint widened to (draw_date, game)")

        rows = [{
            "d": r["draw_date"], "n1": r["numbers"][0], "n2": r["numbers"][1],
            "n3": r["numbers"][2], "n4": r["numbers"][3], "n5": r["numbers"][4],
            "pb": r["special_ball"],
        } for r in dp_rows]
        conn.execute(text("""
            INSERT INTO powerball_draws (draw_date, n1, n2, n3, n4, n5, powerball, game)
            VALUES (:d, :n1, :n2, :n3, :n4, :n5, :pb, 'powerball_doubleplay')
            ON CONFLICT (draw_date, game) DO NOTHING
        """), rows)

        after_dp = conn.execute(text(
            "SELECT count(*) FROM powerball_draws WHERE game = 'powerball_doubleplay'"
        )).scalar()
        total = conn.execute(text("SELECT count(*) FROM powerball_draws")).scalar()
        conn.commit()
        print(f"powerball_draws now has {after_dp} Double Play rows, {total} rows total")


if __name__ == "__main__":
    main()
