"""One-off historical backfill: Powerball draws from data/powerball_2017-2025.csv.

Live draws come from the data.ny.gov sync in api.py; this only fills history.
Safe to re-run -- dates already in powerball_draws are skipped, so it never
duplicates rows (repeated runs of an earlier version of this script are what
repair_data.py had to clean up).

    python3 import_data.py          # uses DATABASE_URL, else local cosmic_lottery_v2
"""

import os
import pandas as pd
from sqlalchemy import create_engine, text

DB_URL = (
    os.environ.get("DATABASE_URL")
    or os.environ.get("DATABASE_PUBLIC_URL")
    or "postgresql://postgres@localhost:5432/cosmic_lottery_v2"
)
# Same driver normalization as the rest of the app (only psycopg2-binary is
# installed; SQLAlchemy 2.1 would otherwise pick psycopg v3 for postgresql://).
DB_URL = DB_URL.replace("postgresql+psycopg://", "postgresql+psycopg2://")
if not DB_URL.startswith("postgresql+psycopg2://"):
    DB_URL = DB_URL.replace("postgresql://", "postgresql+psycopg2://").replace("postgres://", "postgresql+psycopg2://")
engine = create_engine(DB_URL, pool_pre_ping=True, pool_recycle=280)

CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "powerball_2017-2025.csv")

print(f"Importing {os.path.basename(CSV_PATH)} into {engine.url.host or 'local socket'}/{engine.url.database}...")
df = pd.read_csv(CSV_PATH)

rows, skipped = [], 0
for _, row in df.iterrows():
    try:
        rows.append({
            'draw_date': pd.to_datetime(row['Date']).date(),
            'n1': int(row['num1']), 'n2': int(row['num2']),
            'n3': int(row['num3']), 'n4': int(row['num4']),
            'n5': int(row['num5']), 'powerball': int(row['powerball']),
            'multiplier': str(row.get('powerplay', '')),
            'game': 'powerball'
        })
    except (KeyError, ValueError, TypeError):
        skipped += 1

with engine.begin() as conn:
    existing = {r[0] for r in conn.execute(text("SELECT draw_date FROM powerball_draws WHERE game = 'powerball'"))}
    new_rows = [r for r in rows if r['draw_date'] not in existing]
    if new_rows:
        conn.execute(text("""
            INSERT INTO powerball_draws (draw_date, n1, n2, n3, n4, n5, powerball, multiplier, game)
            VALUES (:draw_date, :n1, :n2, :n3, :n4, :n5, :powerball, :multiplier, :game)
        """), new_rows)

print(f"Read {len(rows)} rows ({skipped} unreadable and skipped); "
      f"{len(rows) - len(new_rows)} already present; inserted {len(new_rows)} new Powerball row(s).")
print("ALL DONE!")
