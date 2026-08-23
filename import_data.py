import pandas as pd
from sqlalchemy import create_engine, text
import os

DB_URL = "postgresql://postgres:Rileyrose69!@localhost:5432/cosmic_lottery"
engine = create_engine(DB_URL, pool_pre_ping=True, pool_recycle=280)

data_folder = os.path.expanduser("~/cosmic-lottery-oracle/data")

print("Importing powerball_2017-2025.csv...")
df1 = pd.read_csv(os.path.join(data_folder, "powerball_2017-2025.csv"))
rows1 = []
for _, row in df1.iterrows():
    try:
        rows1.append({
            'draw_date': pd.to_datetime(row['Date']).date(),
            'n1': int(row['num1']), 'n2': int(row['num2']),
            'n3': int(row['num3']), 'n4': int(row['num4']),
            'n5': int(row['num5']), 'powerball': int(row['powerball']),
            'multiplier': str(row.get('powerplay', '')),
            'game': 'powerball'
        })
    except:
        continue
df1_clean = pd.DataFrame(rows1)
df1_clean.to_sql('powerball_draws', engine, if_exists='append', index=False)
print(f"Imported {len(df1_clean)} Powerball rows from 2017-2025 file!")
print("ALL DONE!")
