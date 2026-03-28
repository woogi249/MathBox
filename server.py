"""Token Flex Dashboard — Central aggregation server (FastAPI + aiosqlite + Telegram)."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import aiosqlite
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from schemas import (
    DAILY_LIMIT,
    MILESTONES,
    AlertEvent,
    AlertType,
    RankBoard,
    RankEntry,
    UsagePayload,
    UsageResponse,
)
from telegram_bot import TelegramBot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s — %(message)s",
)
logger = logging.getLogger("tokenflex.server")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_PATH = os.getenv("TOKENFLEX_DB", "tokenflex.db")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

TEMPLATES_DIR = Path(__file__).parent / "templates"

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

_db: aiosqlite.Connection | None = None
_bot: TelegramBot | None = None


async def get_db() -> aiosqlite.Connection:
    assert _db is not None, "DB not initialised"
    return _db


# ---------------------------------------------------------------------------
# Database initialisation
# ---------------------------------------------------------------------------


async def init_db(db: aiosqlite.Connection) -> None:
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS usage (
            user_id       TEXT PRIMARY KEY,
            used_tokens   INTEGER NOT NULL DEFAULT 0,
            last_reported TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS milestones (
            user_id    TEXT NOT NULL,
            milestone  INTEGER NOT NULL,
            reached_at TEXT NOT NULL,
            PRIMARY KEY (user_id, milestone)
        );

        CREATE TABLE IF NOT EXISTS daily_usage (
            user_id TEXT NOT NULL,
            date    TEXT NOT NULL,
            tokens  INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, date)
        );
        """
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Milestone & alert detection
# ---------------------------------------------------------------------------


async def check_milestones(
    db: aiosqlite.Connection, user_id: str, used_tokens: int
) -> list[AlertEvent]:
    events: list[AlertEvent] = []
    for ms in MILESTONES:
        if used_tokens < ms:
            break
        async with db.execute(
            "SELECT 1 FROM milestones WHERE user_id = ? AND milestone = ?",
            (user_id, ms),
        ) as cur:
            if await cur.fetchone():
                continue
        await db.execute(
            "INSERT INTO milestones (user_id, milestone, reached_at) VALUES (?, ?, ?)",
            (user_id, ms, datetime.utcnow().isoformat()),
        )
        events.append(
            AlertEvent(
                alert_type=AlertType.MILESTONE,
                user_id=user_id,
                used_tokens=used_tokens,
                message=f"🎉 *{user_id}* 님이 *{ms:,}* 토큰 마일스톤을 달성했습니다!",
            )
        )
    return events


async def check_daily_limit(
    db: aiosqlite.Connection, user_id: str, used_tokens: int
) -> AlertEvent | None:
    today = datetime.utcnow().strftime("%Y-%m-%d")
    async with db.execute(
        "SELECT tokens FROM daily_usage WHERE user_id = ? AND date = ?",
        (user_id, today),
    ) as cur:
        row = await cur.fetchone()
        prev_tokens = row[0] if row else 0

    await db.execute(
        """
        INSERT INTO daily_usage (user_id, date, tokens) VALUES (?, ?, ?)
        ON CONFLICT(user_id, date) DO UPDATE SET tokens = ?
        """,
        (user_id, today, used_tokens, used_tokens),
    )

    if used_tokens >= DAILY_LIMIT and prev_tokens < DAILY_LIMIT:
        return AlertEvent(
            alert_type=AlertType.DAILY_LIMIT,
            user_id=user_id,
            used_tokens=used_tokens,
            message=f"🔥 *{user_id}* 님이 일일 한도 *{DAILY_LIMIT:,}* 토큰을 돌파했습니다!",
        )
    return None


async def check_rank_change(
    db: aiosqlite.Connection, user_id: str, used_tokens: int
) -> AlertEvent | None:
    async with db.execute(
        "SELECT COUNT(*) FROM usage WHERE used_tokens > ?",
        (used_tokens,),
    ) as cur:
        cnt = (await cur.fetchone())[0]
    if cnt == 0:
        return AlertEvent(
            alert_type=AlertType.RANK_CHANGE,
            user_id=user_id,
            used_tokens=used_tokens,
            message=f"👑 *{user_id}* 님이 *1위*로 등극했습니다! ({used_tokens:,} tokens)",
        )
    return None


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


async def build_rank_board() -> RankBoard:
    db = await get_db()
    async with db.execute(
        "SELECT user_id, used_tokens, last_reported FROM usage ORDER BY used_tokens DESC"
    ) as cur:
        rows = await cur.fetchall()
    board = [
        RankEntry(
            rank=idx,
            user_id=row[0],
            used_tokens=row[1],
            last_reported_at=datetime.fromisoformat(row[2]),
        )
        for idx, row in enumerate(rows, 1)
    ]
    return RankBoard(board=board)


async def get_user_stats(db: aiosqlite.Connection, user_id: str) -> dict | None:
    async with db.execute(
        "SELECT used_tokens, last_reported FROM usage WHERE user_id = ?", (user_id,)
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return None

    # rank
    async with db.execute(
        "SELECT COUNT(*) FROM usage WHERE used_tokens > ?", (row[0],)
    ) as cur:
        rank = (await cur.fetchone())[0] + 1

    async with db.execute("SELECT COUNT(*) FROM usage") as cur:
        total = (await cur.fetchone())[0]

    # milestones
    async with db.execute(
        "SELECT milestone, reached_at FROM milestones WHERE user_id = ? ORDER BY milestone",
        (user_id,),
    ) as cur:
        ms_rows = await cur.fetchall()

    # today's usage
    today = datetime.utcnow().strftime("%Y-%m-%d")
    async with db.execute(
        "SELECT tokens FROM daily_usage WHERE user_id = ? AND date = ?",
        (user_id, today),
    ) as cur:
        d_row = await cur.fetchone()
        today_tokens = d_row[0] if d_row else 0

    return {
        "user_id": user_id,
        "used_tokens": row[0],
        "last_reported": row[1],
        "rank": rank,
        "total_users": total,
        "today_tokens": today_tokens,
        "milestones": [
            {"milestone": m[0], "reached_at": m[1]} for m in ms_rows
        ],
    }


async def get_daily_history(db: aiosqlite.Connection, user_id: str, limit: int = 30) -> list[dict]:
    async with db.execute(
        "SELECT date, tokens FROM daily_usage WHERE user_id = ? ORDER BY date DESC LIMIT ?",
        (user_id, limit),
    ) as cur:
        rows = await cur.fetchall()
    return [{"date": r[0], "tokens": r[1]} for r in reversed(rows)]


async def get_global_stats(db: aiosqlite.Connection) -> dict:
    async with db.execute("SELECT COUNT(*), COALESCE(SUM(used_tokens),0) FROM usage") as cur:
        row = await cur.fetchone()
        total_users, total_tokens = row[0], row[1]

    # leader
    leader = None
    async with db.execute(
        "SELECT user_id, used_tokens FROM usage ORDER BY used_tokens DESC LIMIT 1"
    ) as cur:
        row = await cur.fetchone()
        if row:
            leader = {"user_id": row[0], "used_tokens": row[1]}

    # latest milestone
    latest_ms = None
    async with db.execute(
        "SELECT user_id, milestone, reached_at FROM milestones ORDER BY reached_at DESC LIMIT 1"
    ) as cur:
        row = await cur.fetchone()
        if row:
            latest_ms = {"user_id": row[0], "milestone": row[1], "reached_at": row[2]}

    # today stats
    today = datetime.utcnow().strftime("%Y-%m-%d")
    async with db.execute(
        "SELECT user_id, tokens FROM daily_usage WHERE date = ? ORDER BY tokens DESC LIMIT 1",
        (today,),
    ) as cur:
        row = await cur.fetchone()
        today_leader = {"user_id": row[0], "tokens": row[1]} if row else None

    return {
        "total_users": total_users,
        "total_tokens": total_tokens,
        "leader": leader,
        "latest_milestone": latest_ms,
        "today_leader": today_leader,
        "daily_limit": DAILY_LIMIT,
    }


# ---------------------------------------------------------------------------
# App lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _db, _bot

    _db = await aiosqlite.connect(DB_PATH)
    _db.row_factory = aiosqlite.Row
    await init_db(_db)

    poll_task = None
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        _bot = TelegramBot(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID)
        _bot.set_rank_callback(build_rank_board)
        _bot.set_stats_callback(lambda: get_global_stats(_db))
        _bot.set_user_callback(lambda uid: get_user_stats(_db, uid))
        _bot.set_daily_callback(lambda uid: get_daily_history(_db, uid, 7))
        await _bot.start()
        poll_task = asyncio.create_task(_bot.poll_loop())
        logger.info("Telegram bot enabled")
    else:
        logger.info("Telegram bot disabled (token/chat_id not set)")

    yield

    if _bot:
        await _bot.stop()
    if poll_task:
        poll_task.cancel()
    await _db.close()


app = FastAPI(title="Token Flex Dashboard", lifespan=lifespan)

# ---------------------------------------------------------------------------
# Web Dashboard
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    html = (TEMPLATES_DIR / "dashboard.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------


@app.post("/report", response_model=UsageResponse)
async def report_usage(payload: UsagePayload):
    db = await get_db()

    async with db.execute(
        "SELECT used_tokens FROM usage WHERE user_id = ?", (payload.user_id,)
    ) as cur:
        prev_row = await cur.fetchone()
        prev_tokens = prev_row[0] if prev_row else 0

    await db.execute(
        """
        INSERT INTO usage (user_id, used_tokens, last_reported)
        VALUES (:uid, :tokens, :ts)
        ON CONFLICT(user_id) DO UPDATE SET
            used_tokens   = :tokens,
            last_reported = :ts
        """,
        {
            "uid": payload.user_id,
            "tokens": payload.used_tokens,
            "ts": payload.reported_at.isoformat(),
        },
    )

    alerts: list[AlertEvent] = []
    alerts.extend(await check_milestones(db, payload.user_id, payload.used_tokens))
    daily_alert = await check_daily_limit(db, payload.user_id, payload.used_tokens)
    if daily_alert:
        alerts.append(daily_alert)
    if payload.used_tokens > prev_tokens:
        rank_alert = await check_rank_change(db, payload.user_id, payload.used_tokens)
        if rank_alert:
            alerts.append(rank_alert)

    await db.commit()

    if _bot and alerts:
        for alert in alerts:
            asyncio.create_task(_bot.broadcast_alert(alert))

    async with db.execute(
        "SELECT COUNT(*) FROM usage WHERE used_tokens > ?", (payload.used_tokens,)
    ) as cur:
        rank = (await cur.fetchone())[0] + 1
    async with db.execute("SELECT COUNT(*) FROM usage") as cur:
        total = (await cur.fetchone())[0]

    return UsageResponse(status="ok", rank=rank, total_users=total)


@app.get("/rank", response_model=RankBoard)
async def get_rank():
    return await build_rank_board()


@app.get("/stats")
async def stats():
    db = await get_db()
    return await get_global_stats(db)


@app.get("/user/{user_id}")
async def user_detail(user_id: str):
    db = await get_db()
    result = await get_user_stats(db, user_id)
    if not result:
        raise HTTPException(status_code=404, detail="User not found")
    return result


@app.get("/daily/{user_id}")
async def daily_history(user_id: str, limit: int = 30):
    db = await get_db()
    return await get_daily_history(db, user_id, min(limit, 90))


@app.get("/health")
async def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Run standalone
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
