import requests
from bs4 import BeautifulSoup
import re
from datetime import datetime
from sqlalchemy import create_engine, text

DB_URL = "postgresql://postgres:Rileyrose69!@localhost:5432/cosmic_lottery"
engine = create_engine(DB_URL, pool_pre_ping=True, pool_recycle=280)

# ============================================================
# CREATE TABLE
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
        print("✓ Table ready")

# ============================================================
# SCRAPE LOTTERYUSA.COM
# ============================================================

def scrape_lotteryusa():
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
    }

    draws = []
    pages_to_try = [
        'https://www.lotteryusa.com/florida/lotto/',
        'https://www.lotteryusa.com/florida/lotto/?page=2',
        'https://www.lotteryusa.com/florida/lotto/?page=3',
    ]

    for url in pages_to_try:
        try:
            print(f"Fetching: {url}")
            res = requests.get(url, headers=headers, timeout=15)
            print(f"Status: {res.status_code}")

            if res.status_code != 200:
                continue

            soup = BeautifulSoup(res.content, 'lxml')

            # Try multiple selectors for draw results
            draw_rows = (
                soup.find_all('li', class_=re.compile(r'draw|result|winning')) or
                soup.find_all('tr', class_=re.compile(r'draw|result')) or
                soup.find_all('div', class_=re.compile(r'draw|result|winning-numbers'))
            )

            print(f"Found {len(draw_rows)} potential draw rows")

            for row in draw_rows:
                try:
                    # Find date
                    date_el = (
                        row.find('time') or
                        row.find(class_=re.compile(r'date')) or
                        row.find('td', class_=re.compile(r'date'))
                    )
                    if not date_el:
                        continue

                    date_str = (
                        date_el.get('datetime') or
                        date_el.get('data-date') or
                        date_el.get_text(strip=True)
                    )

                    # Find numbers
                    num_els = (
                        row.find_all(class_=re.compile(r'ball|number|num')) or
                        row.find_all('td', class_=re.compile(r'ball|number'))
                    )

                    nums = []
                    for el in num_els:
                        txt = el.get_text(strip=True)
                        if txt.isdigit() and 1 <= int(txt) <= 53:
                            nums.append(int(txt))

                    if len(nums) >= 6 and date_str:
                        # Parse date
                        for fmt in ['%Y-%m-%d', '%m/%d/%Y', '%B %d, %Y', '%b %d, %Y']:
                            try:
                                parsed_date = datetime.strptime(date_str.strip(), fmt).date()
                                draws.append({
                                    'draw_date': parsed_date,
                                    'n1': nums[0], 'n2': nums[1], 'n3': nums[2],
                                    'n4': nums[3], 'n5': nums[4], 'n6': nums[5],
                                    'game': 'florida_lotto'
                                })
                                break
                            except:
                                continue
                except Exception as e:
                    continue

        except Exception as e:
            print(f"Error fetching {url}: {e}")
            continue

    return draws

# ============================================================
# SCRAPE LOTTERYCORNER.COM (backup)
# ============================================================

def scrape_lotterycorner():
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
    }
    draws = []

    try:
        url = 'https://www.lotterycorner.com/fl/lotto'
        print(f"\nTrying backup: {url}")
        res = requests.get(url, headers=headers, timeout=15)
        print(f"Status: {res.status_code}")

        if res.status_code == 200:
            soup = BeautifulSoup(res.content, 'lxml')

            # Find all tables or result rows
            tables = soup.find_all('table')
            for table in tables:
                rows = table.find_all('tr')
                for row in rows:
                    cells = row.find_all('td')
                    if len(cells) >= 7:
                        try:
                            date_str = cells[0].get_text(strip=True)
                            nums = []
                            for cell in cells[1:7]:
                                txt = cell.get_text(strip=True)
                                if txt.isdigit():
                                    nums.append(int(txt))
                            if len(nums) >= 6:
                                for fmt in ['%m/%d/%Y', '%Y-%m-%d', '%B %d, %Y']:
                                    try:
                                        parsed_date = datetime.strptime(date_str, fmt).date()
                                        draws.append({
                                            'draw_date': parsed_date,
                                            'n1': nums[0], 'n2': nums[1],
                                            'n3': nums[2], 'n4': nums[3],
                                            'n5': nums[4], 'n6': nums[5],
                                            'game': 'florida_lotto'
                                        })
                                        break
                                    except:
                                        continue
                        except:
                            continue
    except Exception as e:
        print(f"Backup scraper error: {e}")

    return draws

# ============================================================
# SAVE TO DATABASE
# ============================================================

def save_draws(draws):
    if not draws:
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
                print(f"Save error: {e}")
        conn.commit()
    return saved

# ============================================================
# CHECK DATABASE
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
        print(f"\n📊 Florida Lotto Database:")
        print(f"   Total draws: {row[0]}")
        print(f"   Earliest: {row[1]}")
        print(f"   Latest: {row[2]}")

        result2 = conn.execute(text("""
            SELECT draw_date, n1, n2, n3, n4, n5, n6
            FROM florida_lotto_draws
            ORDER BY draw_date DESC LIMIT 5
        """))
        print(f"\n   Most recent 5 draws:")
        for r in result2:
            print(f"   {r[0]}: {r[1]}, {r[2]}, {r[3]}, {r[4]}, {r[5]}, {r[6]}")

# ============================================================
# ADD KNOWN RECENT DRAWS MANUALLY
# Update these with real results from flalottery.com
# Florida Lotto draws every Wednesday and Saturday at 11:15 PM
# ============================================================

def add_known_draws():
    """
    Add verified recent Florida Lotto results.
    Get latest from: https://www.flalottery.com/lotto
    Format: ('YYYY-MM-DD', n1, n2, n3, n4, n5, n6)
    """
    known = [
        # Add real recent draws here after checking flalottery.com
        # Example format:
        # ('2026-06-18', 7, 14, 22, 31, 40, 51),
    ]

    if not known:
        return 0

    draws = [{'draw_date': r[0], 'n1': r[1], 'n2': r[2],
              'n3': r[3], 'n4': r[4], 'n5': r[5],
              'n6': r[6], 'game': 'florida_lotto'} for r in known]
    return save_draws(draws)

# ============================================================
# MAIN
# ============================================================

if __name__ == '__main__':
    print("=== Florida Lotto Auto-Updater ===\n")

    create_table()

    # Try primary scraper
    print("\n🔍 Attempting to scrape lotteryusa.com...")
    draws = scrape_lotteryusa()

    if draws:
        print(f"✓ Scraped {len(draws)} draws!")
        saved = save_draws(draws)
        print(f"✓ Saved {saved} new draws")
    else:
        print("Primary scraper found no results, trying backup...")
        draws = scrape_lotterycorner()
        if draws:
            print(f"✓ Backup scraped {len(draws)} draws!")
            saved = save_draws(draws)
            print(f"✓ Saved {saved} new draws")
        else:
            print("⚠ Both scrapers unsuccessful")
            print("→ Please add recent draws manually to add_known_draws()")
            print("→ Get numbers from: https://www.flalottery.com/lotto")

    # Add any manually verified draws
    manual_saved = add_known_draws()
    if manual_saved:
        print(f"✓ Added {manual_saved} manual draws")

    check_database()

    print("\n=== Done! ===")
    print("Run this script after every Wednesday & Saturday draw")
    print("to keep your database current!")