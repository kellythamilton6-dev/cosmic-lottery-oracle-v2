from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from datetime import date, datetime
from typing import Optional
from sqlalchemy import create_engine, text
from cosmic_engine import (
    generate_predictions,
    get_moon_phase,
    get_historical_hot_numbers,
    get_moon_phase_patterns
)

import os
import httpx
import jwt
import base64
import time
import logging
from contextlib import asynccontextmanager
from apscheduler.schedulers.background import BackgroundScheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("sync")

SUPABASE_JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET")

def get_user_id(authorization: Optional[str] = Header(None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.split(" ", 1)[1]
    try:
        payload = jwt.decode(token, options={"verify_signature": False})
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="No user ID in token")
        return user_id
    except jwt.PyJWTError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")

DB_URL = (
    os.environ.get("DATABASE_URL") or
    os.environ.get("DATABASE_PUBLIC_URL") or
    "postgresql://postgres:Rileyrose69!@localhost:5432/cosmic_lottery_v2"
)
engine = create_engine(DB_URL)

# In-memory record of the most recent sync attempt (scheduled or manual),
# so sync health can be checked via /sync-status instead of only Railway logs.
_last_sync = {"ran_at": None, "success": None, "powerball_inserted": None, "megamillions_inserted": None, "error": None}

def scheduled_sync():
    global _last_sync
    started = datetime.utcnow().isoformat()
    try:
        with engine.connect() as conn:
            pb = sync_game('powerball', conn)
            mm = sync_game('megamillions', conn)
            conn.commit()
        _last_sync = {"ran_at": started, "success": True, "powerball_inserted": pb, "megamillions_inserted": mm, "error": None}
        logger.info(f"sync OK: powerball +{pb}, megamillions +{mm}")
    except Exception as e:
        _last_sync = {"ran_at": started, "success": False, "powerball_inserted": None, "megamillions_inserted": None, "error": str(e)}
        logger.error(f"sync FAILED: {e}")

@asynccontextmanager
async def lifespan(app):
    scheduler = BackgroundScheduler()
    # Catch up immediately on every startup/deploy (Railway restarts the
    # process often during active development, which used to mean losing
    # the in-process schedule until the next 9am UTC trigger).
    scheduler.add_job(scheduled_sync, id="startup_sync")
    # Then re-check every 3 hours instead of once a day, so a single missed
    # or failed run (network hiccup, NY Open Data throttling) self-heals
    # within hours instead of silently drifting for days.
    scheduler.add_job(scheduled_sync, 'interval', hours=3, id="periodic_sync")
    scheduler.start()
    yield
    scheduler.shutdown()

app = FastAPI(title="Cosmic Lottery Oracle API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================
# REQUEST MODELS
# ============================================================

class PredictionRequest(BaseModel):
    birth_date: str
    draw_date: Optional[str] = None
    name: Optional[str] = ""
    game: Optional[str] = "powerball"
    w_moon: Optional[float] = 0.30
    w_astro: Optional[float] = 0.25
    w_vedic: Optional[float] = 0.25
    w_num: Optional[float] = 0.20

class SavePredictionRequest(BaseModel):
    birth_date: Optional[str] = None
    draw_date: str
    game: str
    primary_numbers: str
    bonus_number: Optional[int] = None
    moon_phase: str
    sun_sign: str
    nakshatra: str
    life_path: int
    mode: Optional[str] = "cosmic"
    weights_json: Optional[str] = None

class ValidateRequest(BaseModel):
    prediction_id: int
    actual_numbers: str
    actual_bonus: Optional[int] = None

class PatternPredictRequest(BaseModel):
    game: Optional[str] = "powerball"
    w_frequency: Optional[float] = 0.50
    w_overdue: Optional[float] = 0.20
    w_trend: Optional[float] = 0.15
    w_moon: Optional[float] = 0.10
    w_pairs: Optional[float] = 0.05

# ============================================================
# HELPERS
# ============================================================

def get_table(game):
    if game == 'powerball':
        return 'powerball_draws'
    elif game == 'megamillions':
        return 'megamillions_draws'
    return 'powerball_draws'

def get_bonus_col(game):
    if game == 'powerball':
        return 'powerball'
    elif game == 'megamillions':
        return 'megaball'
    return 'powerball'

def get_game_config(game):
    configs = {
        'powerball': {'main': 5, 'max': 69, 'bonus_max': 26},
        'megamillions': {'main': 5, 'max': 70, 'bonus_max': 25},
    }
    return configs.get(game, configs['powerball'])

# ============================================================
# ROUTES
# ============================================================

def _upsert_draw(conn, table, bonus_col, draw_date, numbers, bonus, game_value):
    """Insert one draw row if it isn't already there. Existence is checked
    per (draw_date, game) -- not just draw_date -- since powerball_draws now
    holds both 'powerball' and 'powerball_doubleplay' rows sharing a date."""
    existing = conn.execute(
        text(f"SELECT id FROM {table} WHERE draw_date = :d AND game = :g"),
        {"d": draw_date, "g": game_value},
    ).fetchone()
    if existing:
        return False
    conn.execute(text(f"""
        INSERT INTO {table} (draw_date, n1, n2, n3, n4, n5, {bonus_col}, game)
        VALUES (:d, :n1, :n2, :n3, :n4, :n5, :bonus, :game)
    """), {"d": draw_date, "n1": numbers[0], "n2": numbers[1], "n3": numbers[2],
           "n4": numbers[3], "n5": numbers[4], "bonus": bonus, "game": game_value})
    return True


def sync_game(game: str, conn, limit: int = 40):
    # limit=40 (not just the last handful) so a sync that was missed for a
    # few days still fully catches up in one pass -- the per-row existence
    # check below makes this idempotent, and the unique constraint on
    # (draw_date, game) is a hard backstop against duplicates either way.
    if game == 'powerball':
        url = f"https://data.ny.gov/resource/d6yy-54nr.json?$order=draw_date+DESC&$limit={limit}"
        table = 'powerball_draws'
        bonus_col = 'powerball'
    else:
        url = f"https://data.ny.gov/resource/5xaw-6ayf.json?$order=draw_date+DESC&$limit={limit}"
        table = 'megamillions_draws'
        bonus_col = 'megaball'

    data = None
    last_err = None
    for attempt in range(3):
        try:
            with httpx.Client(timeout=15) as client:
                resp = client.get(url, headers={"User-Agent": "cosmic-lottery-oracle/1.0"})
                resp.raise_for_status()
                data = resp.json()
            break
        except Exception as e:
            last_err = e
            logger.warning(f"{game} sync attempt {attempt + 1}/3 failed: {e}")
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    if data is None:
        raise RuntimeError(f"NY Open Data fetch failed for {game} after 3 attempts: {last_err}")

    inserted = 0
    dp_inserted = 0
    for row in data:
        nums = [int(n) for n in row.get("winning_numbers", "").split()]
        if len(nums) < 5:
            continue
        if game == 'powerball':
            main, bonus = nums[:5], nums[5] if len(nums) == 6 else 0
        else:
            main = nums[:5]
            bonus = int(row.get("mega_ball", 0) or 0)
        draw_date = row.get("draw_date", "")[:10]
        if _upsert_draw(conn, table, bonus_col, draw_date, main, bonus, game):
            inserted += 1

        # NY Open Data includes the same night's Double Play numbers inline
        # on the main Powerball dataset -- no separate feed needed.
        if game == 'powerball':
            dp_raw = row.get("double_play_winning_numbers", "")
            dp_nums = [int(n) for n in dp_raw.split()] if dp_raw else []
            if len(dp_nums) == 6:
                if _upsert_draw(conn, table, bonus_col, draw_date, dp_nums[:5], dp_nums[5], 'powerball_doubleplay'):
                    dp_inserted += 1

    if game == 'powerball':
        logger.info(f"powerball: fetched {len(data)}, inserted {inserted} main + {dp_inserted} double play row(s)")
        return inserted + dp_inserted
    logger.info(f"{game}: fetched {len(data)}, inserted {inserted} new row(s)")
    return inserted

@app.get("/sync-status")
def sync_status():
    return {"success": True, "last_sync": _last_sync}

@app.post("/sync-draws")
def sync_draws():
    try:
        with engine.connect() as conn:
            pb = sync_game('powerball', conn)
            mm = sync_game('megamillions', conn)
            conn.commit()
        return {"success": True, "powerball_inserted": pb, "megamillions_inserted": mm}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/")
def root():
    return {"message": "✦ Cosmic Lottery Oracle API is running!"}

@app.get("/moon")
def moon_today():
    phase = get_moon_phase(date.today())
    return phase

@app.post("/predict")
def predict(req: PredictionRequest):
    try:
        birth = datetime.strptime(req.birth_date, "%Y-%m-%d").date()
        draw = datetime.strptime(req.draw_date, "%Y-%m-%d").date() if req.draw_date else date.today()
        result = generate_predictions(
            birth_date=birth,
            target_date=draw,
            name=req.name or "",
            game=req.game,
            w_moon=req.w_moon,
            w_astro=req.w_astro,
            w_vedic=req.w_vedic,
            w_num=req.w_num
        )
        return {"success": True, "prediction": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

def ensure_predictions_table(conn):
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS predictions (
            id SERIAL PRIMARY KEY,
            user_id UUID,
            birth_date DATE,
            draw_date DATE,
            game VARCHAR(20),
            primary_numbers VARCHAR(100),
            bonus_number INTEGER,
            moon_phase VARCHAR(50),
            sun_sign VARCHAR(20),
            nakshatra VARCHAR(50),
            life_path INTEGER,
            actual_numbers VARCHAR(100),
            actual_bonus INTEGER,
            matches INTEGER,
            validated BOOLEAN DEFAULT FALSE,
            mode VARCHAR(20) DEFAULT 'cosmic',
            weights_json TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """))
    conn.execute(text("ALTER TABLE predictions ADD COLUMN IF NOT EXISTS user_id UUID"))
    conn.execute(text("ALTER TABLE predictions ADD COLUMN IF NOT EXISTS mode VARCHAR(20) DEFAULT 'cosmic'"))
    conn.execute(text("ALTER TABLE predictions ADD COLUMN IF NOT EXISTS weights_json TEXT"))

@app.post("/save-prediction")
def save_prediction(req: SavePredictionRequest, user_id: str = Depends(get_user_id)):
    try:
        with engine.connect() as conn:
            ensure_predictions_table(conn)
            result = conn.execute(text("""
                INSERT INTO predictions
                (user_id, birth_date, draw_date, game, primary_numbers, bonus_number,
                moon_phase, sun_sign, nakshatra, life_path, mode, weights_json)
                VALUES (:user_id, :birth_date, :draw_date, :game, :primary_numbers,
                :bonus_number, :moon_phase, :sun_sign, :nakshatra, :life_path, :mode, :weights_json)
                RETURNING id
            """), {
                "user_id": user_id,
                "birth_date": req.birth_date,
                "draw_date": req.draw_date,
                "game": req.game,
                "primary_numbers": req.primary_numbers,
                "bonus_number": req.bonus_number,
                "moon_phase": req.moon_phase,
                "sun_sign": req.sun_sign,
                "nakshatra": req.nakshatra,
                "life_path": req.life_path,
                "mode": req.mode,
                "weights_json": req.weights_json,
            })
            pred_id = result.fetchone()[0]
            conn.commit()
        return {"success": True, "id": pred_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/predictions")
def list_predictions(user_id: str = Depends(get_user_id)):
    try:
        with engine.connect() as conn:
            ensure_predictions_table(conn)
            rows = conn.execute(text("""
                SELECT id, draw_date, game, primary_numbers, bonus_number,
                       moon_phase, sun_sign, life_path, matches, validated,
                       mode, weights_json
                FROM predictions
                WHERE user_id = :user_id
                ORDER BY created_at DESC
            """), {"user_id": user_id}).fetchall()
        preds = [{
            "id": r[0],
            "draw_date": str(r[1]),
            "game": r[2],
            "primary": [int(n) for n in r[3].split(",")] if r[3] else [],
            "bonus": r[4],
            "moon": r[5],
            "sign": r[6],
            "life_path": r[7],
            "matches": r[8],
            "validated": r[9],
            "mode": r[10] or "cosmic",
            "weights_json": r[11],
        } for r in rows]
        return {"success": True, "predictions": preds}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/validate")
def validate(req: ValidateRequest, user_id: str = Depends(get_user_id)):
    try:
        actual = [int(n) for n in req.actual_numbers.split(",")]
        with engine.connect() as conn:
            ensure_predictions_table(conn)
            result = conn.execute(text(
                "SELECT primary_numbers FROM predictions WHERE id = :id AND user_id = :user_id"
            ), {"id": req.prediction_id, "user_id": user_id})
            row = result.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Prediction not found")
            predicted = [int(n) for n in row[0].split(",")]
            matches = len(set(predicted) & set(actual))
            conn.execute(text("""
                UPDATE predictions SET
                actual_numbers = :actual,
                actual_bonus = :bonus,
                matches = :matches,
                validated = TRUE
                WHERE id = :id AND user_id = :user_id
            """), {
                "actual": req.actual_numbers,
                "bonus": req.actual_bonus,
                "matches": matches,
                "id": req.prediction_id,
                "user_id": user_id
            })
            conn.commit()
        return {
            "success": True,
            "matches": matches,
            "predicted": predicted,
            "actual": actual
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/history/{game}")
def history(game: str, limit: int = 20):
    try:
        if game == 'powerball':
            url = f"https://data.ny.gov/resource/d6yy-54nr.json?$order=draw_date+DESC&$limit={limit}"
        else:
            url = f"https://data.ny.gov/resource/5xaw-6ayf.json?$order=draw_date+DESC&$limit={limit}"

        with httpx.Client(timeout=10) as client:
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.json()

        draws = []
        for row in data:
            nums = [int(n) for n in row.get("winning_numbers", "").split()]
            if game == 'powerball':
                bonus = int(row.get("winning_numbers", "0 0 0 0 0 0").split()[-1]) if len(nums) == 6 else 0
                main = nums[:5] if len(nums) >= 5 else nums
                bonus = nums[5] if len(nums) == 6 else int(row.get("multiplier", 0) or 0)
            else:
                main = nums[:5] if len(nums) >= 5 else nums
                bonus = int(row.get("mega_ball", 0) or 0)
            draws.append({
                "date": row.get("draw_date", "")[:10],
                "numbers": main,
                "bonus": bonus
            })

        return {"success": True, "draws": draws}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/frequency/{game}")
def frequency(game: str):
    try:
        data = get_historical_hot_numbers(game)
        return {
            "success": True,
            "hot": list(data['hot']),
            "cold": list(data['cold']),
            "frequency": {str(k): v for k, v in data['frequency'].items()}
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/pattern-match-draws/{game}")
def pattern_match_draws(game: str, limit: int = 100, include_secondary: bool = True):
    try:
        from match_engine import recent_draws_for_picker
        return {"success": True, "draws": recent_draws_for_picker(game, limit=limit, include_secondary=include_secondary)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/pattern-match/{game}")
def pattern_match_route(
    game: str,
    draw_id: Optional[int] = None,
    numbers: Optional[str] = None,
    bonus: Optional[int] = None,
    limit: int = 10,
    include_secondary: bool = True,
):
    try:
        from match_engine import pattern_match

        custom_numbers = None
        if numbers:
            try:
                custom_numbers = [int(x) for x in numbers.split(",")]
            except ValueError:
                raise HTTPException(status_code=400, detail="numbers must be comma-separated integers")
            if len(custom_numbers) != 5 or len(set(custom_numbers)) != 5:
                raise HTTPException(status_code=400, detail="Provide exactly 5 distinct numbers")

        result = pattern_match(
            game,
            draw_id=draw_id,
            custom_numbers=custom_numbers,
            custom_bonus=bonus,
            limit=min(max(limit, 1), 50),
            include_secondary=include_secondary,
        )
        if result is None:
            raise HTTPException(status_code=404, detail="No data available for this game/draw")
        return {"success": True, "result": result}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/predict-historical")
def predict_historical(req: PredictionRequest):
    try:
        cfg = get_game_config(req.game)
        table = get_table(req.game)
        bonus_col = get_bonus_col(req.game)

        with engine.connect() as conn:
            result = conn.execute(text(f"""
                SELECT draw_date, n1, n2, n3, n4, n5, {bonus_col}
                FROM {table}
                ORDER BY draw_date DESC
            """))
            rows = result.fetchall()

        if not rows:
            raise HTTPException(status_code=404, detail="No historical data found")

        from collections import Counter
        import random

        all_nums = []
        bonus_nums = []
        last_seen = {}

        for i, row in enumerate(rows):
            nums = [row[1], row[2], row[3], row[4], row[5]]
            bonus = row[6]
            all_nums.extend(nums)
            bonus_nums.append(bonus)
            for n in nums:
                if n not in last_seen:
                    last_seen[n] = i
            if bonus not in last_seen:
                last_seen[bonus] = i

        freq = Counter(all_nums)
        bonus_freq = Counter(bonus_nums)
        total_draws = len(rows)
        max_num = cfg['max']

        pool = {}
        for n in range(1, max_num + 1):
            f = freq.get(n, 0)
            freq_score = (f / total_draws) * 300
            draws_since = last_seen.get(n, total_draws)
            overdue_score = (draws_since / total_draws) * 200
            expected = (total_draws * 5) / max_num
            deviation = f - expected
            due_score = max(0, -deviation) * 2
            pool[n] = 10.0 + freq_score + overdue_score + due_score

        def weighted_pick(count):
            available = dict(pool)
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
            bonus_pool = {}
            for n in range(1, cfg['bonus_max'] + 1):
                f = bonus_freq.get(n, 0)
                draws_since = last_seen.get(n, total_draws)
                bonus_pool[n] = 10.0 + (f/total_draws)*300 + (draws_since/total_draws)*200
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

        hot = [n for n, c in freq.most_common(10)]
        cold = [n for n, c in freq.most_common()[:-11:-1]]
        most_overdue = sorted(range(1, max_num+1), key=lambda n: last_seen.get(n, total_draws), reverse=True)[:10]

        return {
            "success": True,
            "mode": "historical",
            "prediction": {
                "primary": primary,
                "alt_a": alt_a,
                "alt_b": alt_b,
                "bonus_primary": bonus_primary,
                "bonus_alt_a": bonus_alt_a,
                "bonus_alt_b": bonus_alt_b,
                "game": req.game,
                "draw_date": req.draw_date or str(date.today()),
                "total_draws_analyzed": total_draws,
                "hot_numbers": hot,
                "cold_numbers": cold,
                "most_overdue": most_overdue,
                "analysis": f"Based on {total_draws} historical {req.game} draws."
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/pattern-analysis")
def pattern_analysis(req: PredictionRequest):
    try:
        from pattern_engine import pattern_predict
        result = pattern_predict(req.game or 'powerball')
        return {"success": True, "result": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/predict-pattern")
def predict_pattern(req: PatternPredictRequest):
    try:
        from pattern_engine import pattern_predict
        weights = {
            'frequency': req.w_frequency,
            'overdue': req.w_overdue,
            'trend': req.w_trend,
            'moon': req.w_moon,
            'pairs': req.w_pairs,
        }
        result = pattern_predict(req.game or 'powerball', weights=weights)
        return {"success": True, "prediction": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))