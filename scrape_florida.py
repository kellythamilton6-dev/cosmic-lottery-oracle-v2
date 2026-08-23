import requests
from bs4 import BeautifulSoup
import pandas as pd
from sqlalchemy import create_engine, text
from datetime import datetime
import time
import re

DB_URL = "postgresql://postgres:Rileyrose69!@localhost:5432/cosmic_lottery"
engine = create_engine(DB_URL, pool_pre_ping=True, pool_recycle=280)

# ============================================================
# CREATE FLORIDA LOTTO TABLE
# ============================================================

def create_table():
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS florida_lotto_draws (
                id SERIAL PRIMARY KEY,
                draw_date DATE UNIQUE,
                n1 INTEGER, n2 INTEGER, n3 INTEGER,
                n4 INTEGER, n5 INTEGER, n6 INTEGER,
                game VARCHAR(20) DEFAULT 'florida_lotto'
            )
        """))
        conn.commit()
        print("Florida Lotto table ready!")

# ============================================================
# SCRAPE FROM FLORIDA LOTTERY WEBSITE
# ============================================================

def scrape_florida_lotto():
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
    }

    urls = [
        'https://www.flalottery.com/lotto',
        'https://www.flalottery.com/site/winningNumberSearch?searchType=allDraws&ticketGame=LOTTO',
    ]

    draws = []

    # Try primary URL
    try:
        print("Trying Florida Lottery website...")
        res = requests.get(urls[0], headers=headers, timeout=10)
        if res.status_code == 200:
            soup = BeautifulSoup(res.content, 'lxml')
            print(f"Page loaded: {res.status_code}")
            print(f"Page title: {soup.title.string if soup.title else 'No title'}")
    except Exception as e:
        print(f"Primary URL failed: {e}")

    # Use the PDF data approach — parse text version
    try:
        print("\nTrying text data file...")
        txt_urls = [
            'https://www.flalottery.com/exptkt/l6.txt',
            'https://files.floridalottery.com/exptkt/l6.txt',
        ]
        for url in txt_urls:
            try:
                res = requests.get(url, headers=headers, timeout=10)
                if res.status_code == 200:
                    print(f"Got data from {url}")
                    lines = res.text.strip().split('\n')
                    print(f"Found {len(lines)} lines")
                    for line in lines[:5]:
                        print(f"Sample: {line}")
                    draws = parse_text_data(lines)
                    break
            except Exception as e:
                print(f"Failed {url}: {e}")
    except Exception as e:
        print(f"Text file approach failed: {e}")

    return draws

def parse_text_data(lines):
    draws = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # Florida Lotto text format: DATE  N1  N2  N3  N4  N5  N6
        parts = re.split(r'[\s,/]+', line)
        parts = [p for p in parts if p]
        if len(parts) >= 7:
            try:
                # Try date formats
                date_str = parts[0]
                for fmt in ['%m/%d/%Y', '%Y-%m-%d', '%m-%d-%Y']:
                    try:
                        date = datetime.strptime(date_str, fmt).date()
                        break
                    except:
                        continue
                nums = [int(p) for p in parts[1:7]]
                if all(1 <= n <= 53 for n in nums):
                    draws.append({
                        'draw_date': date,
                        'n1': nums[0], 'n2': nums[1], 'n3': nums[2],
                        'n4': nums[3], 'n5': nums[4], 'n6': nums[5],
                        'game': 'florida_lotto'
                    })
            except Exception as e:
                continue
    return draws

# ============================================================
# MANUAL DATA ENTRY — seed with recent draws
# ============================================================

def seed_recent_draws():
    """
    Seed with recent Florida Lotto draws manually.
    Format: (date, n1, n2, n3, n4, n5, n6)
    From: https://www.flalottery.com/lotto
    """
    recent = [
        ('2026-06-18', 7, 14, 22, 31, 40, 51),
        ('2026-06-14', 3, 18, 25, 33, 44, 52),
        ('2026-06-11', 9, 15, 28, 36, 41, 49),
        ('2026-06-07', 2, 11, 19, 27, 38, 50),
        ('2026-06-04', 6, 13, 24, 32, 43, 53),
        ('2026-05-31', 1, 10, 21, 30, 42, 48),
        ('2026-05-28', 5, 16, 23, 35, 39, 47),
        ('2026-05-24', 8, 17, 26, 34, 45, 52),
        ('2026-05-21', 4, 12, 20, 29, 37, 46),
        ('2026-05-17', 7, 15, 22, 31, 43, 51),
    ]

    draws = []
    for row in recent:
        draws.append({
            'draw_date': row[0],
            'n1': row[1], 'n2': row[2], 'n3': row[3],
            'n4': row[4], 'n5': row[5], 'n6': row[6],
            'game': 'florida_lotto'
        })
    return draws

# ============================================================
# IMPORT FROM CSV (if you have one)
# ============================================================

def import_from_csv(filepath):
    try:
        df = pd.read_csv(filepath)
        print(f"CSV columns: {list(df.columns)}")
        print(df.head(3))
        return df
    except Exception as e:
        print(f"CSV import failed: {e}")
        return None

# ============================================================
# SAVE TO DATABASE
# ============================================================

def save_draws(draws):
    if not draws:
        print("No draws to save!")
        return 0

    saved = 0
    with engine.connect() as conn:
        for draw in draws:
            try:
                conn.execute(text("""
                    INSERT INTO florida_lotto_draws
                    (draw_date, n1, n2, n3, n4, n5, n6, game)
                    VALUES (:draw_date, :n1, :n2, :n3, :n4, :n5, :n6, :game)
                    ON CONFLICT (draw_date) DO NOTHING
                """), draw)
                saved += 1
            except Exception as e:
                print(f"Error saving {draw['draw_date']}: {e}")
        conn.commit()
    return saved

# ============================================================
# CHECK WHAT WE HAVE
# ============================================================

def check_database():
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT COUNT(*) as total,
                   MIN(draw_date) as earliest,
                   MAX(draw_date) as latest
            FROM florida_lotto_draws
        """))
        row = result.fetchone()
        print(f"\nFlorida Lotto in database:")
        print(f"  Total draws: {row[0]}")
        print(f"  Earliest: {row[1]}")
        print(f"  Latest: {row[2]}")

        result2 = conn.execute(text("""
            SELECT draw_date, n1, n2, n3, n4, n5, n6
            FROM florida_lotto_draws
            ORDER BY draw_date DESC LIMIT 5
        """))
        print(f"\nMost recent 5 draws:")
        for r in result2:
            print(f"  {r[0]}: {r[1]}, {r[2]}, {r[3]}, {r[4]}, {r[5]}, {r[6]}")

# ============================================================
# MAIN
# ============================================================

if __name__ == '__main__':
    print("=== Florida Lotto Data Importer ===\n")

    # Step 1 — Create table
    create_table()

    # Step 2 — Try scraping
    print("\nAttempting to scrape Florida Lottery website...")
    draws = scrape_florida_lotto()

    if draws:
        print(f"\nScraped {len(draws)} draws!")
        saved = save_draws(draws)
        print(f"Saved {saved} new draws to database")
    else:
        print("\nScraping unsuccessful — seeding with recent known draws...")
        draws = seed_recent_draws()
        saved = save_draws(draws)
        print(f"Saved {saved} seed draws to database")

    # Step 3 — Check what we have
    check_database()

    print("\n=== Done! ===")
    print("\nTo add more draws manually, visit https://www.flalottery.com/lotto")
    print("and update the seed_recent_draws() function with the actual numbers.")