"""Token Flex Dashboard — Central aggregation server (FastAPI + aiosqlite + Telegram)."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime

import aiosqlite
from fastapi import FastAPI

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
            user_id   TEXT NOT NULL,
            milestone INTEGER NOT NULL,
            reached_at TEXT NOT NULL,
            PRIMARY KEY (user_id, milestone)
        );

        CREATE TABLE IF NOT EXISTS daily_usage (
            user_id    TEXT NOT NULL,
            date       TEXT NOT NULL,
            tokens     INTEGER NOT NULL DEFAULT 0,
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
    """유저의 토큰 사용량에 대해 마일스톤 돌파 여부를 검사한다."""
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
        # 신규 마일스톤 달성
        await db.execute(
            "INSERT INTO milestones (user_id, milestone, reached_at) VALUES (?, ?, ?)",
            (user_id, ms, datetime.utcnow().isoformat()),
        )
        ms_label = f"{ms:,}"
        events.append(
            AlertEvent(
                alert_type=AlertType.MILESTONE,
                user_id=user_id,
                used_tokens=used_tokens,
                message=(
                    f"🎉 *{user_id}* 님이 *{ms_label}* 토큰 마일스톤을 달성했습니다!"
                ),
            )
        )

    return events


async def check_daily_limit(
    db: aiosqlite.Connection, user_id: str, used_tokens: int
) -> AlertEvent | None:
    """일일 사용량 한도 돌파 여부를 검사한다."""
    today = datetime.utcnow().strftime("%Y-%m-%d")

    async with db.execute(
        "SELECT tokens FROM daily_usage WHERE user_id = ? AND date = ?",
        (user_id, today),
    ) as cur:
        row = await cur.fetchone()
        prev_tokens = row[0] if row else 0

    # 일일 사용량 갱신 (누적 토큰의 차이가 아닌, 보고된 절대값 기준 간이 추적)
    await db.execute(
        """
        INSERT INTO daily_usage (user_id, date, tokens)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id, date) DO UPDATE SET tokens = ?
        """,
        (user_id, today, used_tokens, used_tokens),
    )

    if used_tokens >= DAILY_LIMIT and prev_tokens < DAILY_LIMIT:
        return AlertEvent(
            alert_type=AlertType.DAILY_LIMIT,
            user_id=user_id,
            used_tokens=used_tokens,
            message=(
                f"🔥 *{user_id}* 님이 일일 한도 *{DAILY_LIMIT:,}* 토큰을 돌파했습니다!"
            ),
        )
    return None


async def check_rank_change(
    db: aiosqlite.Connection, user_id: str, used_tokens: int
) -> AlertEvent | None:
    """1위 등극 시 알림."""
    async with db.execute(
        "SELECT user_id FROM usage ORDER BY used_tokens DESC LIMIT 1"
    ) as cur:
        row = await cur.fetchone()

    if row and row[0] == user_id:
        # 이미 1위인지 확인 — 방금 역전한 경우만 알림
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
# Ranking helper (Telegram 콜백용)
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


# ---------------------------------------------------------------------------
# App lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _db, _bot

    # DB
    _db = await aiosqlite.connect(DB_PATH)
    _db.row_factory = aiosqlite.Row
    await init_db(_db)

    # Telegram bot (토큰 미설정 시 비활성)
    poll_task = None
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        _bot = TelegramBot(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID)
        _bot.set_rank_callback(build_rank_board)
        await _bot.start()
        poll_task = asyncio.create_task(_bot.poll_loop())
        logger.info("Telegram bot enabled")
    else:
        logger.info("Telegram bot disabled (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set)")

    yield

    # Shutdown
    if _bot:
        await _bot.stop()
    if poll_task:
        poll_task.cancel()
    await _db.close()


app = FastAPI(title="Token Flex Dashboard", lifespan=lifespan)

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.post("/report", response_model=UsageResponse)
async def report_usage(payload: UsagePayload):
    """클라이언트가 10분마다 호출하는 사용량 보고 엔드포인트."""
    db = await get_db()

    # 이전 토큰 값 조회 (1위 역전 감지용)
    async with db.execute(
        "SELECT used_tokens FROM usage WHERE user_id = ?",
        (payload.user_id,),
    ) as cur:
        prev_row = await cur.fetchone()
        prev_tokens = prev_row[0] if prev_row else 0

    # UPSERT
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

    # 마일스톤 & 한도 검사
    alerts: list[AlertEvent] = []
    alerts.extend(await check_milestones(db, payload.user_id, payload.used_tokens))
    daily_alert = await check_daily_limit(db, payload.user_id, payload.used_tokens)
    if daily_alert:
        alerts.append(daily_alert)

    # 1위 역전 검사 (토큰이 증가한 경우에만)
    if payload.used_tokens > prev_tokens:
        rank_alert = await check_rank_change(db, payload.user_id, payload.used_tokens)
        if rank_alert:
            alerts.append(rank_alert)

    await db.commit()

    # Telegram 알림 비동기 발송
    if _bot and alerts:
        for alert in alerts:
            asyncio.create_task(_bot.broadcast_alert(alert))

    # 현재 유저 랭킹 계산
    async with db.execute(
        "SELECT COUNT(*) FROM usage WHERE used_tokens > ?",
        (payload.used_tokens,),
    ) as cur:
        rank = (await cur.fetchone())[0] + 1

    async with db.execute("SELECT COUNT(*) FROM usage") as cur:
        total = (await cur.fetchone())[0]

    return UsageResponse(status="ok", rank=rank, total_users=total)


@app.get("/rank", response_model=RankBoard)
async def get_rank():
    """전체 랭킹 보드 조회."""
    return await build_rank_board()


@app.get("/health")
async def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Run standalone
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
