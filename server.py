"""Token Flex Dashboard — Central aggregation server (FastAPI + aiosqlite)."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime

import aiosqlite
from fastapi import FastAPI, HTTPException

from schemas import RankBoard, RankEntry, UsagePayload, UsageResponse

DB_PATH = os.getenv("TOKENFLEX_DB", "tokenflex.db")

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

_db: aiosqlite.Connection | None = None


async def get_db() -> aiosqlite.Connection:
    assert _db is not None, "DB not initialised"
    return _db


async def init_db(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS usage (
            user_id       TEXT PRIMARY KEY,
            used_tokens   INTEGER NOT NULL DEFAULT 0,
            last_reported TEXT NOT NULL
        )
        """
    )
    await db.commit()


# ---------------------------------------------------------------------------
# App lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _db
    _db = await aiosqlite.connect(DB_PATH)
    _db.row_factory = aiosqlite.Row
    await init_db(_db)
    yield
    await _db.close()


app = FastAPI(title="Token Flex Dashboard", lifespan=lifespan)

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.post("/report", response_model=UsageResponse)
async def report_usage(payload: UsagePayload):
    """클라이언트가 10분마다 호출하는 사용량 보고 엔드포인트."""
    db = await get_db()
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
    await db.commit()

    # 현재 유저 랭킹 계산
    async with db.execute(
        "SELECT COUNT(*) FROM usage WHERE used_tokens > ?",
        (payload.used_tokens,),
    ) as cur:
        row = await cur.fetchone()
        rank = row[0] + 1

    async with db.execute("SELECT COUNT(*) FROM usage") as cur:
        total = (await cur.fetchone())[0]

    return UsageResponse(status="ok", rank=rank, total_users=total)


@app.get("/rank", response_model=RankBoard)
async def get_rank():
    """전체 랭킹 보드 조회."""
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
# Run standalone
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
