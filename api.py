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
engine = create_engine(DB_URL, pool_pre_ping=True, pool_recycle=280)

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
    draw_type: Optional[str] = "main"
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

@app.get("/draw/{game}/{draw_date}")
def get_draw_by_date(game: str, draw_date: str):
    """Look up actual winning numbers for a game and date.
    If no draw on that exact date, finds the closest draw within 3 days."""
    table = 'megamillions_draws' if game == 'megamillions' else 'powerball_draws'
    bonus_col = 'megaball' if game == 'megamillions' else 'powerball'
    try:
        with engine.connect() as conn:
            # First try exact date
            row = conn.execute(text(f"""
                SELECT draw_date, n1, n2, n3, n4, n5, {bonus_col}
                FROM {table}
                WHERE draw_date = :d
                ORDER BY id DESC LIMIT 1
            """), {"d": draw_date}).fetchone()
            # Fall back to nearest draw within 3 days
            if not row:
                row = conn.execute(text(f"""
                    SELECT draw_date, n1, n2, n3, n4, n5, {bonus_col}
                    FROM {table}
                    WHERE draw_date BETWEEN CAST(:d_minus AS date) - INTERVAL '3 days' AND CAST(:d_plus AS date) + INTERVAL '3 days'
                    AND draw_date <= CURRENT_DATE
                    ORDER BY ABS(draw_date - CAST(:d_target AS date)) ASC
                    LIMIT 1
                """), {"d_minus": draw_date, "d_plus": draw_date, "d_target": draw_date}).fetchone()
        if not row:
            return {"success": False, "error": f"No {game} draw found near {draw_date}"}
        actual_date = str(row[0])
        return {
            "success": True,
            "game": game,
            "date": actual_date,
            "requested_date": draw_date,
            "nearest": actual_date != draw_date,
            "numbers": sorted([row[1], row[2], row[3], row[4], row[5]]),
            "bonus": row[6]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

def ensure_pattern_snapshots_table(conn):
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS pattern_snapshots (
            id SERIAL PRIMARY KEY,
            user_id TEXT NOT NULL,
            game TEXT NOT NULL,
            draw_type TEXT NOT NULL DEFAULT 'main',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            snapshot JSONB NOT NULL
        )
    """))
    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS idx_pattern_snapshots_user_game
        ON pattern_snapshots (user_id, game, created_at DESC)
    """))

class SaveSnapshotRequest(BaseModel):
    game: str
    draw_type: Optional[str] = 'main'
    snapshot: dict

@app.post("/pattern-snapshot/save")
def save_pattern_snapshot(req: SaveSnapshotRequest, user_id: str = Depends(get_user_id)):
    try:
        import json
        with engine.connect() as conn:
            ensure_pattern_snapshots_table(conn)
            conn.execute(text("""
                INSERT INTO pattern_snapshots (user_id, game, draw_type, snapshot)
                VALUES (:user_id, :game, :draw_type, CAST(:snapshot AS jsonb))
            """), {
                "user_id": user_id,
                "game": req.game,
                "draw_type": req.draw_type or 'main',
                "snapshot": json.dumps(req.snapshot),
            })
            conn.commit()
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/pattern-snapshot/history/{game}")
def get_pattern_snapshots(game: str, limit: int = 8, user_id: str = Depends(get_user_id)):
    try:
        with engine.connect() as conn:
            ensure_pattern_snapshots_table(conn)
            rows = conn.execute(text("""
                SELECT id, game, draw_type, created_at, snapshot
                FROM pattern_snapshots
                WHERE user_id = :user_id AND game = :game
                ORDER BY created_at DESC
                LIMIT :limit
            """), {"user_id": user_id, "game": game, "limit": limit}).fetchall()
        snapshots = []
        for row in rows:
            snapshots.append({
                "id": row[0],
                "game": row[1],
                "draw_type": row[2],
                "created_at": row[3].isoformat(),
                "snapshot": row[4],
            })
        return {"success": True, "snapshots": snapshots}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/auto-validate-pending")
def auto_validate_pending(user_id: str = Depends(get_user_id)):
    """Find all unvalidated predictions and compare against the NEXT draw on or after draw_date."""
    try:
        with engine.connect() as conn:
            ensure_predictions_table(conn)
            rows = conn.execute(text("""
                SELECT id, draw_date, game, primary_numbers, bonus_number
                FROM predictions
                WHERE user_id = :user_id AND (validated = FALSE OR validated IS NULL)
                ORDER BY draw_date ASC
            """), {"user_id": user_id}).fetchall()

            updated = 0
            results = []
            for r in rows:
                pred_id, draw_date, game, primary_numbers, bonus_number = r
                table = 'megamillions_draws' if game == 'megamillions' else 'powerball_draws'
                bonus_col = 'megaball' if game == 'megamillions' else 'powerball'

                # Find NEXT draw on or after the prediction's draw_date
                draw_row = conn.execute(text(f"""
                    SELECT draw_date, n1, n2, n3, n4, n5, {bonus_col}
                    FROM {table}
                    WHERE draw_date >= CAST(:d AS date)
                    AND draw_date <= CURRENT_DATE
                    ORDER BY draw_date ASC
                    LIMIT 1
                """), {"d": str(draw_date)}).fetchone()

                if not draw_row:
                    results.append({"id": pred_id, "status": "no_draw_yet"})
                    continue

                actual_date = str(draw_row[0])
                actual_nums = sorted([draw_row[1], draw_row[2], draw_row[3], draw_row[4], draw_row[5]])
                actual_bonus = draw_row[6]
                actual_str = ",".join(str(n) for n in actual_nums)

                predicted = [int(n) for n in primary_numbers.split(",")]
                matches = len(set(predicted) & set(actual_nums))
                bonus_match = bonus_number == actual_bonus if bonus_number is not None else False

                conn.execute(text("""
                    UPDATE predictions SET
                        actual_numbers = :actual,
                        actual_bonus = :bonus,
                        matches = :matches,
                        validated = TRUE
                    WHERE id = :id AND user_id = :user_id
                """), {
                    "actual": actual_str,
                    "bonus": actual_bonus,
                    "matches": matches,
                    "id": pred_id,
                    "user_id": user_id,
                })
                updated += 1
                results.append({
                    "id": pred_id,
                    "draw_date": str(draw_date),
                    "actual_draw_date": actual_date,
                    "matches": matches,
                    "bonus_match": bonus_match,
                    "status": "validated",
                })

            conn.commit()
        return {"success": True, "updated": updated, "results": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

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

        # No WHERE filter here meant Main and Double Play draws were always
        # blended together, unlike pattern_engine.py's load_draws() (fixed
        # earlier) which keeps them as separate homogeneous streams. Mega
        # Millions has no Double Play, so draw_type is forced back to
        # 'main' for it regardless of what was requested.
        draw_type = req.draw_type if (req.draw_type == 'doubleplay' and req.game != 'megamillions') else 'main'
        game_value = f"{req.game}_doubleplay" if draw_type == 'doubleplay' else req.game

        with engine.connect() as conn:
            result = conn.execute(text(f"""
                SELECT draw_date, n1, n2, n3, n4, n5, {bonus_col}
                FROM {table}
                WHERE game = :game_value
                ORDER BY draw_date DESC
            """), {"game_value": game_value})
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
                "draw_type": draw_type,
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
        result = pattern_predict(req.game or 'powerball', draw_type=req.draw_type or 'main')
        return {"success": True, "result": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ============================================================
# TSF (TRANSITION & STRUCTURAL FORECASTING MODEL)
# ============================================================

class TsfCommitRequest(BaseModel):
    game: str = "powerball"
    draw_type: str = "main"

class TsfScoreRequest(BaseModel):
    id: Optional[int] = None
    game: Optional[str] = None
    draw_type: Optional[str] = None

TSF_CREATE_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS tsf_forecasts (
        id SERIAL PRIMARY KEY,
        game VARCHAR(30) NOT NULL,
        draw_type VARCHAR(20) NOT NULL DEFAULT 'main',
        model_version VARCHAR(20) NOT NULL,
        as_of_draw_date DATE NOT NULL,
        target_draw_date DATE NOT NULL,
        committed_at TIMESTAMP DEFAULT NOW(),
        structural_state JSONB NOT NULL,
        persistent_zones JSONB NOT NULL,
        transitions JSONB NOT NULL,
        hypotheses JSONB NOT NULL,
        lines JSONB NOT NULL,
        actual_numbers VARCHAR(100),
        actual_draw_date DATE,
        scored BOOLEAN DEFAULT FALSE,
        scorecard JSONB,
        scored_at TIMESTAMP,
        UNIQUE (game, draw_type, target_draw_date)
    )
"""

@app.get("/tsf/forecast")
def tsf_forecast_route(game: str = "powerball", draw_type: str = "main"):
    try:
        from tsf_engine import tsf_forecast
        result = tsf_forecast(game, draw_type)
        if 'error' in result:
            raise HTTPException(status_code=404, detail=result['error'])
        return {"success": True, "result": result}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/tsf/commit")
def tsf_commit(req: TsfCommitRequest):
    try:
        from tsf_engine import tsf_forecast, MODEL_VERSION
        import json as _json

        result = tsf_forecast(req.game, req.draw_type)
        if 'error' in result:
            raise HTTPException(status_code=404, detail=result['error'])

        as_of = datetime.strptime(result['as_of_draw_date'], "%Y-%m-%d").date()
        target = datetime.strptime(result['target_draw_date'], "%Y-%m-%d").date()
        if target <= as_of:
            raise HTTPException(status_code=400, detail="Target draw date is not after the most recent known draw")

        with engine.connect() as conn:
            conn.execute(text(TSF_CREATE_TABLE_SQL))
            existing = conn.execute(text("""
                SELECT id, committed_at FROM tsf_forecasts
                WHERE game = :game AND draw_type = :draw_type AND target_draw_date = :target
            """), {"game": req.game, "draw_type": req.draw_type, "target": target}).fetchone()
            if existing:
                conn.commit()
                raise HTTPException(
                    status_code=409,
                    detail=f"Forecast already committed for {target} (id={existing[0]}, committed {existing[1]})"
                )
            row = conn.execute(text("""
                INSERT INTO tsf_forecasts
                (game, draw_type, model_version, as_of_draw_date, target_draw_date,
                 structural_state, persistent_zones, transitions, hypotheses, lines)
                VALUES (:game, :draw_type, :model_version, :as_of, :target,
                 CAST(:structural_state AS JSONB), CAST(:persistent_zones AS JSONB),
                 CAST(:transitions AS JSONB), CAST(:hypotheses AS JSONB), CAST(:lines AS JSONB))
                RETURNING id
            """), {
                "game": req.game, "draw_type": req.draw_type, "model_version": MODEL_VERSION,
                "as_of": as_of, "target": target,
                "structural_state": _json.dumps(result['current_state']),
                "persistent_zones": _json.dumps(result['persistent_zones']),
                "transitions": _json.dumps(result['transitions']),
                "hypotheses": _json.dumps(result['hypotheses']),
                "lines": _json.dumps(result['lines']),
            })
            new_id = row.fetchone()[0]
            conn.commit()

        return {"success": True, "id": new_id, "target_draw_date": str(target), "forecast": result}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/tsf/score")
def tsf_score(req: TsfScoreRequest):
    try:
        from tsf_engine import score_forecast, sum_regime_cutoffs, get_config as _get_config
        from pattern_engine import load_draws as _load_draws
        import json as _json

        with engine.connect() as conn:
            conn.execute(text(TSF_CREATE_TABLE_SQL))

            where = ["scored = FALSE"]
            params = {}
            if req.id is not None:
                where.append("id = :id"); params["id"] = req.id
            if req.game:
                where.append("game = :game"); params["game"] = req.game
            if req.draw_type:
                where.append("draw_type = :draw_type"); params["draw_type"] = req.draw_type

            pending = conn.execute(text(f"""
                SELECT id, game, draw_type, target_draw_date, lines
                FROM tsf_forecasts WHERE {' AND '.join(where)}
            """), params).fetchall()

            scored_results = []
            still_pending = 0
            for fid, fgame, fdraw_type, ftarget, flines in pending:
                cfg = _get_config(fgame)
                table = cfg["table"]
                dp_value = cfg["doubleplay_game_value"]
                game_value = dp_value if (fdraw_type == "doubleplay" and dp_value) else fgame

                actual = conn.execute(text(f"""
                    SELECT n1, n2, n3, n4, n5 FROM {table}
                    WHERE game = :game_value AND draw_date = :target
                """), {"game_value": game_value, "target": ftarget}).fetchone()

                if not actual:
                    still_pending += 1
                    continue

                actual_numbers = list(actual)
                all_draws = _load_draws(fgame, draw_type=fdraw_type)
                cutoffs = sum_regime_cutoffs(all_draws)

                scorecard = score_forecast(flines, actual_numbers, cfg["max_num"], cfg["main_count"], cutoffs)

                conn.execute(text("""
                    UPDATE tsf_forecasts SET
                    actual_numbers = :actual_numbers, actual_draw_date = :target,
                    scored = TRUE, scorecard = CAST(:scorecard AS JSONB), scored_at = NOW()
                    WHERE id = :id
                """), {
                    "actual_numbers": ",".join(str(n) for n in sorted(actual_numbers)),
                    "target": ftarget, "scorecard": _json.dumps(scorecard), "id": fid
                })
                scored_results.append({"id": fid, "scorecard": scorecard})

            conn.commit()

        return {"success": True, "scored": scored_results, "still_pending": still_pending}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/tsf/history")
def tsf_history(game: str = "powerball", draw_type: str = "main", limit: int = 50):
    try:
        with engine.connect() as conn:
            conn.execute(text(TSF_CREATE_TABLE_SQL))
            conn.commit()
            rows = conn.execute(text("""
                SELECT id, game, draw_type, model_version, as_of_draw_date, target_draw_date,
                       committed_at, hypotheses, lines, actual_numbers, actual_draw_date,
                       scored, scorecard, scored_at
                FROM tsf_forecasts
                WHERE game = :game AND draw_type = :draw_type
                ORDER BY committed_at DESC LIMIT :limit
            """), {"game": game, "draw_type": draw_type, "limit": limit}).fetchall()
        return {"success": True, "forecasts": [
            {
                "id": r[0], "game": r[1], "draw_type": r[2], "model_version": r[3],
                "as_of_draw_date": str(r[4]), "target_draw_date": str(r[5]),
                "committed_at": str(r[6]), "hypotheses": r[7], "lines": r[8],
                "actual_numbers": r[9], "actual_draw_date": str(r[10]) if r[10] else None,
                "scored": r[11], "scorecard": r[12],
                "scored_at": str(r[13]) if r[13] else None,
            } for r in rows
        ]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/tsf/track-record")
def tsf_track_record(game: str = "powerball", draw_type: str = "main"):
    try:
        from tsf_engine import track_record_summary, MODEL_VERSION
        with engine.connect() as conn:
            conn.execute(text(TSF_CREATE_TABLE_SQL))
            conn.commit()
            rows = conn.execute(text("""
                SELECT model_version, scorecard FROM tsf_forecasts
                WHERE game = :game AND draw_type = :draw_type AND scored = TRUE
            """), {"game": game, "draw_type": draw_type}).fetchall()
        scored_rows = [{"model_version": r[0], "scorecard": r[1]} for r in rows]
        summary = track_record_summary(scored_rows, MODEL_VERSION)
        return {"success": True, "summary": summary}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/tsf/calibration-report")
def tsf_calibration_report(game: str = "powerball", draw_type: str = "main", months: int = 12):
    try:
        from tsf_engine import calibration_report
        result = calibration_report(game, draw_type, months)
        if 'error' in result:
            raise HTTPException(status_code=404, detail=result['error'])
        return {"success": True, "report": result}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/tsf/line-backtest-report")
def tsf_line_backtest_report(game: str = "powerball", draw_type: str = "main", months: int = 12, samples_per_draw: int = 10):
    try:
        from tsf_engine import line_backtest_report
        result = line_backtest_report(game, draw_type, months, samples_per_draw=samples_per_draw)
        if 'error' in result:
            raise HTTPException(status_code=404, detail=result['error'])
        return {"success": True, "report": result}
    except HTTPException:
        raise
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