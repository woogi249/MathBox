"""Token Flex Dashboard — Central aggregation server (FastAPI + aiosqlite + Telegram)."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
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
    JoinRequest,
    MemberInfo,
    MemberStatus,
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
OPEN_REGISTRATION = os.getenv("TOKENFLEX_OPEN_REG", "false").lower() == "true"

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

        CREATE TABLE IF NOT EXISTS members (
            user_id          TEXT PRIMARY KEY,
            display_name     TEXT NOT NULL DEFAULT '',
            telegram_chat_id TEXT NOT NULL DEFAULT '',
            invited_by       TEXT NOT NULL DEFAULT '',
            status           TEXT NOT NULL DEFAULT 'active',
            joined_at        TEXT NOT NULL,
            left_at          TEXT
        );

        CREATE TABLE IF NOT EXISTS invites (
            code       TEXT PRIMARY KEY,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            used_by    TEXT NOT NULL DEFAULT '',
            used       INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Member management helpers
# ---------------------------------------------------------------------------


async def is_active_member(db: aiosqlite.Connection, user_id: str) -> bool:
    async with db.execute(
        "SELECT 1 FROM members WHERE user_id = ? AND status = 'active'",
        (user_id,),
    ) as cur:
        return await cur.fetchone() is not None


async def register_member(
    db: aiosqlite.Connection,
    user_id: str,
    display_name: str = "",
    telegram_chat_id: str = "",
    invited_by: str = "",
) -> MemberInfo:
    now = datetime.utcnow().isoformat()

    # 이전에 탈퇴한 유저 → 재가입
    async with db.execute(
        "SELECT status FROM members WHERE user_id = ?", (user_id,)
    ) as cur:
        row = await cur.fetchone()

    if row:
        await db.execute(
            """
            UPDATE members
            SET status = 'active', display_name = ?, telegram_chat_id = ?,
                invited_by = ?, joined_at = ?, left_at = NULL
            WHERE user_id = ?
            """,
            (display_name, telegram_chat_id, invited_by, now, user_id),
        )
    else:
        await db.execute(
            """
            INSERT INTO members (user_id, display_name, telegram_chat_id, invited_by, status, joined_at)
            VALUES (?, ?, ?, ?, 'active', ?)
            """,
            (user_id, display_name, telegram_chat_id, invited_by, now),
        )
    await db.commit()

    return MemberInfo(
        user_id=user_id,
        display_name=display_name,
        telegram_chat_id=telegram_chat_id,
        invited_by=invited_by,
        status=MemberStatus.ACTIVE,
        joined_at=datetime.fromisoformat(now),
    )


async def leave_member(db: aiosqlite.Connection, user_id: str) -> bool:
    async with db.execute(
        "SELECT 1 FROM members WHERE user_id = ? AND status = 'active'",
        (user_id,),
    ) as cur:
        if not await cur.fetchone():
            return False

    now = datetime.utcnow().isoformat()
    await db.execute(
        "UPDATE members SET status = 'left', left_at = ? WHERE user_id = ?",
        (now, user_id),
    )
    await db.commit()
    return True


async def create_invite(db: aiosqlite.Connection, created_by: str) -> str:
    code = secrets.token_urlsafe(8)
    now = datetime.utcnow().isoformat()
    await db.execute(
        "INSERT INTO invites (code, created_by, created_at) VALUES (?, ?, ?)",
        (code, created_by, now),
    )
    await db.commit()
    return code


async def consume_invite(db: aiosqlite.Connection, code: str, used_by: str) -> str | None:
    """초대코드 사용. 성공 시 초대자 user_id 반환, 실패 시 None."""
    async with db.execute(
        "SELECT created_by, used FROM invites WHERE code = ?", (code,)
    ) as cur:
        row = await cur.fetchone()
    if not row or row[1]:
        return None
    await db.execute(
        "UPDATE invites SET used = 1, used_by = ? WHERE code = ?",
        (used_by, code),
    )
    await db.commit()
    return row[0]


async def list_active_members(db: aiosqlite.Connection) -> list[dict]:
    async with db.execute(
        "SELECT user_id, display_name, invited_by, joined_at FROM members WHERE status = 'active' ORDER BY joined_at"
    ) as cur:
        rows = await cur.fetchall()
    return [
        {"user_id": r[0], "display_name": r[1], "invited_by": r[2], "joined_at": r[3]}
        for r in rows
    ]


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
    # 활성 멤버만 랭킹에 포함
    async with db.execute(
        """
        SELECT u.user_id, u.used_tokens, u.last_reported
        FROM usage u
        INNER JOIN members m ON u.user_id = m.user_id AND m.status = 'active'
        ORDER BY u.used_tokens DESC
        """
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

    async with db.execute(
        "SELECT COUNT(*) FROM usage WHERE used_tokens > ?", (row[0],)
    ) as cur:
        rank = (await cur.fetchone())[0] + 1

    async with db.execute(
        "SELECT COUNT(*) FROM members WHERE status = 'active'"
    ) as cur:
        total = (await cur.fetchone())[0]

    async with db.execute(
        "SELECT milestone, reached_at FROM milestones WHERE user_id = ? ORDER BY milestone",
        (user_id,),
    ) as cur:
        ms_rows = await cur.fetchall()

    today = datetime.utcnow().strftime("%Y-%m-%d")
    async with db.execute(
        "SELECT tokens FROM daily_usage WHERE user_id = ? AND date = ?",
        (user_id, today),
    ) as cur:
        d_row = await cur.fetchone()
        today_tokens = d_row[0] if d_row else 0

    # member info
    async with db.execute(
        "SELECT display_name, invited_by, status, joined_at FROM members WHERE user_id = ?",
        (user_id,),
    ) as cur:
        m_row = await cur.fetchone()

    return {
        "user_id": user_id,
        "display_name": m_row[0] if m_row else "",
        "used_tokens": row[0],
        "last_reported": row[1],
        "rank": rank,
        "total_users": total,
        "today_tokens": today_tokens,
        "milestones": [{"milestone": m[0], "reached_at": m[1]} for m in ms_rows],
        "invited_by": m_row[1] if m_row else "",
        "status": m_row[2] if m_row else "unknown",
        "joined_at": m_row[3] if m_row else "",
    }


async def get_daily_history(
    db: aiosqlite.Connection, user_id: str, limit: int = 30
) -> list[dict]:
    async with db.execute(
        "SELECT date, tokens FROM daily_usage WHERE user_id = ? ORDER BY date DESC LIMIT ?",
        (user_id, limit),
    ) as cur:
        rows = await cur.fetchall()
    return [{"date": r[0], "tokens": r[1]} for r in reversed(rows)]


async def get_global_stats(db: aiosqlite.Connection) -> dict:
    async with db.execute(
        "SELECT COUNT(*) FROM members WHERE status = 'active'"
    ) as cur:
        total_users = (await cur.fetchone())[0]

    async with db.execute("SELECT COALESCE(SUM(used_tokens),0) FROM usage") as cur:
        total_tokens = (await cur.fetchone())[0]

    leader = None
    async with db.execute(
        """
        SELECT u.user_id, u.used_tokens FROM usage u
        INNER JOIN members m ON u.user_id = m.user_id AND m.status = 'active'
        ORDER BY u.used_tokens DESC LIMIT 1
        """
    ) as cur:
        row = await cur.fetchone()
        if row:
            leader = {"user_id": row[0], "used_tokens": row[1]}

    latest_ms = None
    async with db.execute(
        "SELECT user_id, milestone, reached_at FROM milestones ORDER BY reached_at DESC LIMIT 1"
    ) as cur:
        row = await cur.fetchone()
        if row:
            latest_ms = {"user_id": row[0], "milestone": row[1], "reached_at": row[2]}

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
        _bot.set_join_callback(
            lambda uid, name, chat_id, code: _handle_join(_db, uid, name, chat_id, code)
        )
        _bot.set_leave_callback(lambda uid: _handle_leave(_db, uid))
        _bot.set_invite_callback(lambda uid: _handle_invite(_db, uid))
        _bot.set_members_callback(lambda: list_active_members(_db))
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


# ---------------------------------------------------------------------------
# Join / Leave / Invite handlers (shared between API and Telegram)
# ---------------------------------------------------------------------------


async def _handle_join(
    db: aiosqlite.Connection,
    user_id: str,
    display_name: str,
    telegram_chat_id: str,
    invite_code: str,
) -> dict:
    """가입 처리. 성공 시 {"ok": True, ...}, 실패 시 {"ok": False, "error": ...}."""
    # 이미 활성 멤버인지 확인
    if await is_active_member(db, user_id):
        return {"ok": False, "error": "이미 참여 중인 멤버입니다."}

    invited_by = ""
    if not OPEN_REGISTRATION:
        if not invite_code:
            return {"ok": False, "error": "초대 코드가 필요합니다. 기존 멤버에게 `/invite`를 요청하세요."}
        inviter = await consume_invite(db, invite_code, user_id)
        if not inviter:
            return {"ok": False, "error": "유효하지 않거나 이미 사용된 초대 코드입니다."}
        invited_by = inviter

    member = await register_member(db, user_id, display_name, telegram_chat_id, invited_by)

    # 그룹 알림
    if _bot:
        invite_line = f" (초대: {invited_by})" if invited_by else ""
        asyncio.create_task(
            _bot.send_message(
                f"🎊 *{display_name or user_id}* 님이 Token Flex에 참여했습니다!{invite_line}"
            )
        )

    return {
        "ok": True,
        "user_id": member.user_id,
        "display_name": member.display_name,
        "invited_by": invited_by,
    }


async def _handle_leave(db: aiosqlite.Connection, user_id: str) -> dict:
    success = await leave_member(db, user_id)
    if not success:
        return {"ok": False, "error": "활성 멤버가 아닙니다."}

    if _bot:
        asyncio.create_task(
            _bot.send_message(f"👋 *{user_id}* 님이 Token Flex를 떠났습니다.")
        )

    return {"ok": True, "user_id": user_id}


async def _handle_invite(db: aiosqlite.Connection, user_id: str) -> dict:
    if not await is_active_member(db, user_id):
        return {"ok": False, "error": "멤버만 초대 코드를 생성할 수 있습니다."}
    code = await create_invite(db, user_id)
    return {"ok": True, "code": code, "created_by": user_id}


app = FastAPI(title="Token Flex Dashboard", lifespan=lifespan)

# ---------------------------------------------------------------------------
# Web Dashboard
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    html = (TEMPLATES_DIR / "dashboard.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


# ---------------------------------------------------------------------------
# Member management API
# ---------------------------------------------------------------------------


@app.post("/join")
async def api_join(req: JoinRequest):
    db = await get_db()
    result = await _handle_join(
        db, req.user_id, req.display_name, req.telegram_chat_id, req.invite_code
    )
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.post("/leave/{user_id}")
async def api_leave(user_id: str):
    db = await get_db()
    result = await _handle_leave(db, user_id)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.get("/members")
async def api_members():
    db = await get_db()
    members = await list_active_members(db)
    return {"members": members, "count": len(members)}


@app.post("/invite/{user_id}")
async def api_invite(user_id: str):
    db = await get_db()
    result = await _handle_invite(db, user_id)
    if not result["ok"]:
        raise HTTPException(status_code=403, detail=result["error"])
    return result


# ---------------------------------------------------------------------------
# Usage API
# ---------------------------------------------------------------------------


@app.post("/report", response_model=UsageResponse)
async def report_usage(payload: UsagePayload):
    db = await get_db()

    # 멤버십 체크
    if not await is_active_member(db, payload.user_id):
        raise HTTPException(
            status_code=403,
            detail=f"'{payload.user_id}'는 활성 멤버가 아닙니다. /join으로 먼저 참여하세요.",
        )

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
    async with db.execute(
        "SELECT COUNT(*) FROM members WHERE status = 'active'"
    ) as cur:
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
