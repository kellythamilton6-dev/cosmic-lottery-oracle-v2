"""One-off repair for two pre-existing data quality issues found while
building the Pattern Match tab:

  1. powerball_draws has duplicate rows per draw_date (up to 3x), left over
     from repeated bulk imports. Verified every duplicate set for a given
     date shares the same sorted numbers + powerball (cross-checked against
     the official Florida Lottery draw history) -- safe to dedupe by
     keeping the lowest id per date.

  2. megamillions_draws is corrupted: only n1 and draw_date were ever
     populated correctly (n2-n5 and megaball are 0 on every row), and every
     date is duplicated 2x. Re-imports cleanly from
     data/megamillions_winning_numbers.csv, which has the correct values
     for the same 648 dates already in the table.

Both tables get a UNIQUE constraint on draw_date afterward so this can't
silently recur.

Usage:
    DATABASE_URL=... python3 repair_data.py            # dry run, prints counts only
    DATABASE_URL=... python3 repair_data.py --apply     # actually writes
"""

import csv
import os
import sys
from datetime import datetime
from sqlalchemy import create_engine, text

DB_URL = (
    os.environ.get("DATABASE_URL")
    or os.environ.get("DATABASE_PUBLIC_URL")
    or "postgresql://postgres:Rileyrose69!@localhost:5432/cosmic_lottery_v2"
)
engine = create_engine(DB_URL)

APPLY = "--apply" in sys.argv


def repair_powerball(conn):
    before = conn.execute(text("SELECT count(*) FROM powerball_draws")).scalar()
    dupes = conn.execute(text("""
        SELECT count(*) FROM powerball_draws p
        WHERE EXISTS (
          SELECT 1 FROM powerball_draws p2 WHERE p2.draw_date = p.draw_date AND p2.id < p.id
        )
    """)).scalar()
    print(f"powerball_draws: {before} rows, {dupes} duplicate rows to remove")
    if not APPLY:
        return
    conn.execute(text("""
        DELETE FROM powerball_draws p
        USING powerball_draws p2
        WHERE p.draw_date = p2.draw_date AND p.id > p2.id
    """))
    existing = conn.execute(text("""
        SELECT constraint_name FROM information_schema.table_constraints
        WHERE table_name = 'powerball_draws' AND constraint_type = 'UNIQUE'
    """)).fetchall()
    if not existing:
        conn.execute(text("ALTER TABLE powerball_draws ADD CONSTRAINT powerball_draws_date_unique UNIQUE (draw_date)"))
    after = conn.execute(text("SELECT count(*) FROM powerball_draws")).scalar()
    print(f"  -> now {after} rows, unique constraint on draw_date ensured")


def repair_megamillions(conn):
    before = conn.execute(text("SELECT count(*) FROM megamillions_draws")).scalar()
    corrupted = conn.execute(text(
        "SELECT count(*) FROM megamillions_draws WHERE n2 = 0 AND n3 = 0 AND n4 = 0"
    )).scalar()
    clean = before - corrupted
    print(f"megamillions_draws: {before} rows ({corrupted} corrupted, {clean} clean/legitimate) "
          f"-> will replace only the corrupted rows from CSV, leaving clean rows untouched")
    if not APPLY:
        return

    csv_path = os.path.join(os.path.dirname(__file__), "data", "megamillions_winning_numbers.csv")
    rows = []
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            draw_date = datetime.strptime(row["drawdate"].strip(), "%m/%d/%Y").date()
            nums = sorted(int(row[f"ball{i}"].strip()) for i in range(1, 6))
            megaball = int(row["megaball"].strip())
            megaplier = (row.get("megaplier") or "").strip()
            rows.append({
                "d": draw_date, "n1": nums[0], "n2": nums[1], "n3": nums[2],
                "n4": nums[3], "n5": nums[4], "mb": megaball, "mult": megaplier,
            })

    # Only remove rows that are actually corrupted -- never touch legitimately
    # synced rows (e.g. recent draws already correctly loaded by the daily sync).
    conn.execute(text("DELETE FROM megamillions_draws WHERE n2 = 0 AND n3 = 0 AND n4 = 0"))

    existing = conn.execute(text("""
        SELECT constraint_name FROM information_schema.table_constraints
        WHERE table_name = 'megamillions_draws' AND constraint_type = 'UNIQUE'
    """)).fetchall()
    if not existing:
        conn.execute(text("ALTER TABLE megamillions_draws ADD CONSTRAINT megamillions_draws_date_unique UNIQUE (draw_date)"))

    # ON CONFLICT DO NOTHING as a defensive no-op in case a CSV date somehow
    # already exists as a clean row -- should never trigger given the CSV's
    # date range, but costs nothing to be safe.
    conn.execute(text("""
        INSERT INTO megamillions_draws (draw_date, n1, n2, n3, n4, n5, megaball, multiplier, game)
        VALUES (:d, :n1, :n2, :n3, :n4, :n5, :mb, :mult, 'megamillions')
        ON CONFLICT (draw_date) DO NOTHING
    """), rows)

    after = conn.execute(text("SELECT count(*) FROM megamillions_draws")).scalar()
    print(f"  -> re-imported {len(rows)} rows, now {after} rows, unique constraint on draw_date ensured")


def main():
    target = DB_URL.split("@")[-1] if "@" in DB_URL else DB_URL
    mode = "APPLYING CHANGES" if APPLY else "DRY RUN (pass --apply to write)"
    print(f"=== {mode} against {target} ===\n")
    with engine.connect() as conn:
        repair_powerball(conn)
        repair_megamillions(conn)
        if APPLY:
            conn.commit()
            print("\nCommitted.")


if __name__ == "__main__":
    main()
