"""Token Flex Dashboard — Central aggregation server (FastAPI + aiosqlite + Telegram).

Role-based group management:
  - owner:  그룹 생성자. 모든 권한 (approve/reject/kick/setadmin/setrole)
  - admin:  부관리자. approve/reject/kick 가능
  - member: 일반 멤버. 자기 정보 조회, 초대코드 생성, 탈퇴
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import base64
import hashlib
import hmac
import json as _json

import aiosqlite
import bcrypt
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse

from schemas import (
    DAILY_LIMIT,
    MILESTONES,
    AlertEvent,
    AlertType,
    JoinRequest,
    LoginRequest,
    MemberRole,
    MemberStatus,
    RankBoard,
    RankEntry,
    RegisterRequest,
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
GROUP_OWNER_ID = os.getenv("TOKENFLEX_OWNER", "")  # 최초 그룹장 user_id

TEMPLATES_DIR = Path(__file__).parent / "templates"

# Session / JWT settings
SESSION_SECRET = os.getenv("TOKENFLEX_SECRET", "")
if not SESSION_SECRET:
    SESSION_SECRET = secrets.token_urlsafe(32)
    logger.warning("TOKENFLEX_SECRET not set — using random key (sessions won't survive restart)")
SESSION_EXPIRE_DAYS = 7

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

_db: aiosqlite.Connection | None = None
_bot: TelegramBot | None = None


async def get_db() -> aiosqlite.Connection:
    assert _db is not None, "DB not initialised"
    return _db


# ---------------------------------------------------------------------------
# Session / Auth helpers
# ---------------------------------------------------------------------------


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64d(s: str) -> bytes:
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s)


def create_session_token(user_id: str) -> str:
    """HMAC-SHA256 signed session token (no external JWT library needed)."""
    from datetime import timedelta
    payload = _json.dumps({
        "sub": user_id,
        "exp": (datetime.utcnow() + timedelta(days=SESSION_EXPIRE_DAYS)).isoformat(),
    }).encode()
    payload_b64 = _b64e(payload)
    sig = hmac.new(SESSION_SECRET.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{sig}"


def decode_session_token(token: str) -> str | None:
    try:
        parts = token.split(".")
        if len(parts) != 2:
            return None
        payload_b64, sig = parts
        expected_sig = hmac.new(SESSION_SECRET.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return None
        payload = _json.loads(_b64d(payload_b64))
        if datetime.fromisoformat(payload["exp"]) < datetime.utcnow():
            return None
        return payload.get("sub")
    except Exception:
        return None


def get_current_user(request: Request) -> str | None:
    token = request.cookies.get("tf_session")
    if not token:
        return None
    return decode_session_token(token)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


# ---------------------------------------------------------------------------
# Database init
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
            role             TEXT NOT NULL DEFAULT 'member',
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

        CREATE TABLE IF NOT EXISTS audit_log (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            actor     TEXT NOT NULL,
            action    TEXT NOT NULL,
            target    TEXT NOT NULL DEFAULT '',
            detail    TEXT NOT NULL DEFAULT '',
            ts        TEXT NOT NULL
        );
        """
    )
    await db.commit()

    # password_hash 컬럼 마이그레이션
    async with db.execute("PRAGMA table_info(members)") as cur:
        cols = [row[1] for row in await cur.fetchall()]
    if "password_hash" not in cols:
        await db.execute("ALTER TABLE members ADD COLUMN password_hash TEXT NOT NULL DEFAULT ''")
        await db.commit()
        logger.info("Added password_hash column to members table")

    # 그룹장 자동 등록 (최초 1회)
    if GROUP_OWNER_ID:
        async with db.execute(
            "SELECT 1 FROM members WHERE user_id = ?", (GROUP_OWNER_ID,)
        ) as cur:
            if not await cur.fetchone():
                now = datetime.utcnow().isoformat()
                await db.execute(
                    """
                    INSERT INTO members (user_id, display_name, role, status, joined_at)
                    VALUES (?, ?, 'owner', 'active', ?)
                    """,
                    (GROUP_OWNER_ID, GROUP_OWNER_ID, now),
                )
                await db.commit()
                logger.info("Group owner '%s' auto-registered", GROUP_OWNER_ID)


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


async def audit(db: aiosqlite.Connection, actor: str, action: str, target: str = "", detail: str = ""):
    await db.execute(
        "INSERT INTO audit_log (actor, action, target, detail, ts) VALUES (?,?,?,?,?)",
        (actor, action, target, detail, datetime.utcnow().isoformat()),
    )


# ---------------------------------------------------------------------------
# Permission helpers
# ---------------------------------------------------------------------------


async def get_member_role(db: aiosqlite.Connection, user_id: str) -> str | None:
    """활성 멤버의 role 반환. 비멤버면 None."""
    async with db.execute(
        "SELECT role FROM members WHERE user_id = ? AND status = 'active'",
        (user_id,),
    ) as cur:
        row = await cur.fetchone()
    return row[0] if row else None


async def is_active_member(db: aiosqlite.Connection, user_id: str) -> bool:
    return await get_member_role(db, user_id) is not None


async def is_admin_or_owner(db: aiosqlite.Connection, user_id: str) -> bool:
    role = await get_member_role(db, user_id)
    return role in ("owner", "admin")


async def is_owner(db: aiosqlite.Connection, user_id: str) -> bool:
    role = await get_member_role(db, user_id)
    return role == "owner"


# ---------------------------------------------------------------------------
# Member management
# ---------------------------------------------------------------------------


async def register_member(
    db: aiosqlite.Connection,
    user_id: str,
    display_name: str = "",
    telegram_chat_id: str = "",
    invited_by: str = "",
    status: str = "active",
    role: str = "member",
) -> dict:
    now = datetime.utcnow().isoformat()

    async with db.execute(
        "SELECT status FROM members WHERE user_id = ?", (user_id,)
    ) as cur:
        row = await cur.fetchone()

    if row:
        if row[0] == "active":
            return {"ok": False, "error": "이미 활성 멤버입니다."}
        if row[0] == "pending":
            return {"ok": False, "error": "이미 참여 신청 중입니다. 관리자 승인을 기다려주세요."}
        # left / kicked → 재가입
        await db.execute(
            """
            UPDATE members SET status = ?, role = ?, display_name = ?,
                telegram_chat_id = ?, invited_by = ?, joined_at = ?, left_at = NULL
            WHERE user_id = ?
            """,
            (status, role, display_name, telegram_chat_id, invited_by, now, user_id),
        )
    else:
        await db.execute(
            """
            INSERT INTO members (user_id, display_name, telegram_chat_id, invited_by, role, status, joined_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, display_name, telegram_chat_id, invited_by, role, status, now),
        )
    await db.commit()
    return {"ok": True, "user_id": user_id, "status": status, "role": role, "invited_by": invited_by}


async def leave_member(db: aiosqlite.Connection, user_id: str) -> dict:
    role = await get_member_role(db, user_id)
    if not role:
        return {"ok": False, "error": "활성 멤버가 아닙니다."}
    if role == "owner":
        return {"ok": False, "error": "그룹장은 탈퇴할 수 없습니다. 먼저 다른 멤버에게 그룹장을 위임하세요."}

    now = datetime.utcnow().isoformat()
    await db.execute(
        "UPDATE members SET status = 'left', left_at = ? WHERE user_id = ?",
        (now, user_id),
    )
    await audit(db, user_id, "leave")
    await db.commit()
    return {"ok": True, "user_id": user_id}


async def kick_member(db: aiosqlite.Connection, actor: str, target: str, reason: str = "") -> dict:
    if not await is_admin_or_owner(db, actor):
        return {"ok": False, "error": "관리자만 추방할 수 있습니다."}

    target_role = await get_member_role(db, target)
    if not target_role:
        return {"ok": False, "error": f"'{target}'는 활성 멤버가 아닙니다."}
    if target_role == "owner":
        return {"ok": False, "error": "그룹장은 추방할 수 없습니다."}
    if target_role == "admin" and not await is_owner(db, actor):
        return {"ok": False, "error": "관리자를 추방하려면 그룹장 권한이 필요합니다."}

    now = datetime.utcnow().isoformat()
    await db.execute(
        "UPDATE members SET status = 'kicked', left_at = ? WHERE user_id = ?",
        (now, target),
    )
    await audit(db, actor, "kick", target, reason)
    await db.commit()
    return {"ok": True, "user_id": target, "kicked_by": actor, "reason": reason}


async def approve_member(db: aiosqlite.Connection, actor: str, target: str) -> dict:
    if not await is_admin_or_owner(db, actor):
        return {"ok": False, "error": "관리자만 승인할 수 있습니다."}

    async with db.execute(
        "SELECT status FROM members WHERE user_id = ?", (target,)
    ) as cur:
        row = await cur.fetchone()
    if not row or row[0] != "pending":
        return {"ok": False, "error": f"'{target}'는 대기 중인 신청이 아닙니다."}

    now = datetime.utcnow().isoformat()
    await db.execute(
        "UPDATE members SET status = 'active', joined_at = ? WHERE user_id = ?",
        (now, target),
    )
    await audit(db, actor, "approve", target)
    await db.commit()
    return {"ok": True, "user_id": target, "approved_by": actor}


async def reject_member(db: aiosqlite.Connection, actor: str, target: str, reason: str = "") -> dict:
    if not await is_admin_or_owner(db, actor):
        return {"ok": False, "error": "관리자만 거절할 수 있습니다."}

    async with db.execute(
        "SELECT status FROM members WHERE user_id = ?", (target,)
    ) as cur:
        row = await cur.fetchone()
    if not row or row[0] != "pending":
        return {"ok": False, "error": f"'{target}'는 대기 중인 신청이 아닙니다."}

    await db.execute("DELETE FROM members WHERE user_id = ?", (target,))
    await audit(db, actor, "reject", target, reason)
    await db.commit()
    return {"ok": True, "user_id": target, "rejected_by": actor}


async def set_role(db: aiosqlite.Connection, actor: str, target: str, new_role: str) -> dict:
    if not await is_owner(db, actor):
        return {"ok": False, "error": "그룹장만 역할을 변경할 수 있습니다."}
    if new_role not in ("admin", "member"):
        return {"ok": False, "error": "역할은 'admin' 또는 'member'만 가능합니다."}
    if target == actor:
        return {"ok": False, "error": "자신의 역할은 변경할 수 없습니다."}

    target_role = await get_member_role(db, target)
    if not target_role:
        return {"ok": False, "error": f"'{target}'는 활성 멤버가 아닙니다."}

    await db.execute("UPDATE members SET role = ? WHERE user_id = ?", (new_role, target))
    await audit(db, actor, "set_role", target, new_role)
    await db.commit()
    return {"ok": True, "user_id": target, "new_role": new_role}


async def transfer_owner(db: aiosqlite.Connection, actor: str, target: str) -> dict:
    if not await is_owner(db, actor):
        return {"ok": False, "error": "그룹장만 위임할 수 있습니다."}
    target_role = await get_member_role(db, target)
    if not target_role:
        return {"ok": False, "error": f"'{target}'는 활성 멤버가 아닙니다."}

    await db.execute("UPDATE members SET role = 'member' WHERE user_id = ?", (actor,))
    await db.execute("UPDATE members SET role = 'owner' WHERE user_id = ?", (target,))
    await audit(db, actor, "transfer_owner", target)
    await db.commit()
    return {"ok": True, "new_owner": target, "prev_owner": actor}


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
        """
        SELECT user_id, display_name, invited_by, role, joined_at
        FROM members WHERE status = 'active' ORDER BY
            CASE role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END,
            joined_at
        """
    ) as cur:
        rows = await cur.fetchall()
    return [
        {"user_id": r[0], "display_name": r[1], "invited_by": r[2], "role": r[3], "joined_at": r[4]}
        for r in rows
    ]


async def list_pending_members(db: aiosqlite.Connection) -> list[dict]:
    async with db.execute(
        "SELECT user_id, display_name, invited_by, joined_at FROM members WHERE status = 'pending' ORDER BY joined_at"
    ) as cur:
        rows = await cur.fetchall()
    return [
        {"user_id": r[0], "display_name": r[1], "invited_by": r[2], "requested_at": r[3]}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Milestone & alert detection
# ---------------------------------------------------------------------------


async def check_milestones(db: aiosqlite.Connection, user_id: str, used_tokens: int) -> list[AlertEvent]:
    events: list[AlertEvent] = []
    for ms in MILESTONES:
        if used_tokens < ms:
            break
        async with db.execute(
            "SELECT 1 FROM milestones WHERE user_id = ? AND milestone = ?", (user_id, ms)
        ) as cur:
            if await cur.fetchone():
                continue
        await db.execute(
            "INSERT INTO milestones (user_id, milestone, reached_at) VALUES (?, ?, ?)",
            (user_id, ms, datetime.utcnow().isoformat()),
        )
        events.append(AlertEvent(
            alert_type=AlertType.MILESTONE, user_id=user_id, used_tokens=used_tokens,
            message=f"🎉 *{user_id}* 님이 *{ms:,}* 토큰 마일스톤을 달성했습니다!",
        ))
    return events


async def check_daily_limit(db: aiosqlite.Connection, user_id: str, used_tokens: int) -> AlertEvent | None:
    today = datetime.utcnow().strftime("%Y-%m-%d")
    async with db.execute(
        "SELECT tokens FROM daily_usage WHERE user_id = ? AND date = ?", (user_id, today)
    ) as cur:
        row = await cur.fetchone()
        prev = row[0] if row else 0
    await db.execute(
        "INSERT INTO daily_usage (user_id, date, tokens) VALUES (?,?,?) ON CONFLICT(user_id, date) DO UPDATE SET tokens = ?",
        (user_id, today, used_tokens, used_tokens),
    )
    if used_tokens >= DAILY_LIMIT and prev < DAILY_LIMIT:
        return AlertEvent(
            alert_type=AlertType.DAILY_LIMIT, user_id=user_id, used_tokens=used_tokens,
            message=f"🔥 *{user_id}* 님이 일일 한도 *{DAILY_LIMIT:,}* 토큰을 돌파했습니다!",
        )
    return None


async def check_rank_change(db: aiosqlite.Connection, user_id: str, used_tokens: int) -> AlertEvent | None:
    async with db.execute("SELECT COUNT(*) FROM usage WHERE used_tokens > ?", (used_tokens,)) as cur:
        cnt = (await cur.fetchone())[0]
    if cnt == 0:
        return AlertEvent(
            alert_type=AlertType.RANK_CHANGE, user_id=user_id, used_tokens=used_tokens,
            message=f"👑 *{user_id}* 님이 *1위*로 등극했습니다! ({used_tokens:,} tokens)",
        )
    return None


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


async def build_rank_board() -> RankBoard:
    db = await get_db()
    async with db.execute(
        """
        SELECT u.user_id, u.used_tokens, u.last_reported
        FROM usage u INNER JOIN members m ON u.user_id = m.user_id AND m.status = 'active'
        ORDER BY u.used_tokens DESC
        """
    ) as cur:
        rows = await cur.fetchall()
    return RankBoard(board=[
        RankEntry(rank=i, user_id=r[0], used_tokens=r[1], last_reported_at=datetime.fromisoformat(r[2]))
        for i, r in enumerate(rows, 1)
    ])


async def get_user_stats(db: aiosqlite.Connection, user_id: str) -> dict | None:
    async with db.execute("SELECT used_tokens, last_reported FROM usage WHERE user_id = ?", (user_id,)) as cur:
        row = await cur.fetchone()
    if not row:
        return None

    async with db.execute("SELECT COUNT(*) FROM usage WHERE used_tokens > ?", (row[0],)) as cur:
        rank = (await cur.fetchone())[0] + 1
    async with db.execute("SELECT COUNT(*) FROM members WHERE status = 'active'") as cur:
        total = (await cur.fetchone())[0]
    async with db.execute(
        "SELECT milestone, reached_at FROM milestones WHERE user_id = ? ORDER BY milestone", (user_id,)
    ) as cur:
        ms_rows = await cur.fetchall()

    today = datetime.utcnow().strftime("%Y-%m-%d")
    async with db.execute("SELECT tokens FROM daily_usage WHERE user_id = ? AND date = ?", (user_id, today)) as cur:
        d_row = await cur.fetchone()

    async with db.execute(
        "SELECT display_name, invited_by, role, status, joined_at FROM members WHERE user_id = ?", (user_id,)
    ) as cur:
        m = await cur.fetchone()

    return {
        "user_id": user_id,
        "display_name": m[0] if m else "",
        "used_tokens": row[0],
        "last_reported": row[1],
        "rank": rank,
        "total_users": total,
        "today_tokens": d_row[0] if d_row else 0,
        "milestones": [{"milestone": r[0], "reached_at": r[1]} for r in ms_rows],
        "invited_by": m[1] if m else "",
        "role": m[2] if m else "",
        "status": m[3] if m else "",
        "joined_at": m[4] if m else "",
    }


async def get_daily_history(db: aiosqlite.Connection, user_id: str, limit: int = 30) -> list[dict]:
    async with db.execute(
        "SELECT date, tokens FROM daily_usage WHERE user_id = ? ORDER BY date DESC LIMIT ?", (user_id, limit)
    ) as cur:
        rows = await cur.fetchall()
    return [{"date": r[0], "tokens": r[1]} for r in reversed(rows)]


async def get_global_stats(db: aiosqlite.Connection) -> dict:
    async with db.execute("SELECT COUNT(*) FROM members WHERE status = 'active'") as cur:
        total_users = (await cur.fetchone())[0]
    async with db.execute("SELECT COALESCE(SUM(used_tokens),0) FROM usage") as cur:
        total_tokens = (await cur.fetchone())[0]

    leader = None
    async with db.execute(
        "SELECT u.user_id, u.used_tokens FROM usage u INNER JOIN members m ON u.user_id = m.user_id AND m.status = 'active' ORDER BY u.used_tokens DESC LIMIT 1"
    ) as cur:
        row = await cur.fetchone()
        if row:
            leader = {"user_id": row[0], "used_tokens": row[1]}

    latest_ms = None
    async with db.execute("SELECT user_id, milestone, reached_at FROM milestones ORDER BY reached_at DESC LIMIT 1") as cur:
        row = await cur.fetchone()
        if row:
            latest_ms = {"user_id": row[0], "milestone": row[1], "reached_at": row[2]}

    today = datetime.utcnow().strftime("%Y-%m-%d")
    async with db.execute(
        "SELECT user_id, tokens FROM daily_usage WHERE date = ? ORDER BY tokens DESC LIMIT 1", (today,)
    ) as cur:
        row = await cur.fetchone()
        today_leader = {"user_id": row[0], "tokens": row[1]} if row else None

    # pending count
    async with db.execute("SELECT COUNT(*) FROM members WHERE status = 'pending'") as cur:
        pending_count = (await cur.fetchone())[0]

    return {
        "total_users": total_users,
        "total_tokens": total_tokens,
        "leader": leader,
        "latest_milestone": latest_ms,
        "today_leader": today_leader,
        "daily_limit": DAILY_LIMIT,
        "pending_count": pending_count,
    }


# ---------------------------------------------------------------------------
# Join / Leave handlers
# ---------------------------------------------------------------------------


async def _handle_join(
    db: aiosqlite.Connection, user_id: str, display_name: str,
    telegram_chat_id: str, invite_code: str,
) -> dict:
    if invite_code:
        inviter = await consume_invite(db, invite_code, user_id)
        if not inviter:
            return {"ok": False, "error": "유효하지 않거나 이미 사용된 초대코드입니다."}
        # 초대코드로 가입 → 즉시 active
        result = await register_member(db, user_id, display_name, telegram_chat_id, inviter, "active")
        if result["ok"] and _bot:
            asyncio.create_task(_bot.send_message(
                f"🎊 *{display_name or user_id}* 님이 Token Flex에 참여했습니다! (초대: {inviter})"
            ))
        return result
    else:
        # 초대코드 없이 → pending (관리자 승인 필요)
        result = await register_member(db, user_id, display_name, telegram_chat_id, "", "pending")
        if result["ok"] and _bot:
            asyncio.create_task(_bot.send_message(
                f"📋 *{display_name or user_id}* 님이 참여를 신청했습니다.\n관리자: `/approve {user_id}` 또는 `/reject {user_id}`"
            ))
        return result


async def _handle_leave(db: aiosqlite.Connection, user_id: str) -> dict:
    result = await leave_member(db, user_id)
    if result["ok"] and _bot:
        asyncio.create_task(_bot.send_message(f"👋 *{user_id}* 님이 Token Flex를 떠났습니다."))
    return result


async def _handle_invite(db: aiosqlite.Connection, user_id: str) -> dict:
    if not await is_active_member(db, user_id):
        return {"ok": False, "error": "멤버만 초대코드를 생성할 수 있습니다."}
    code = await create_invite(db, user_id)
    return {"ok": True, "code": code, "created_by": user_id}


async def _handle_kick(db: aiosqlite.Connection, actor: str, target: str, reason: str = "") -> dict:
    result = await kick_member(db, actor, target, reason)
    if result["ok"] and _bot:
        reason_txt = f" (사유: {reason})" if reason else ""
        asyncio.create_task(_bot.send_message(
            f"🚫 *{target}* 님이 *{actor}* 에 의해 추방되었습니다.{reason_txt}"
        ))
    return result


async def _handle_approve(db: aiosqlite.Connection, actor: str, target: str) -> dict:
    result = await approve_member(db, actor, target)
    if result["ok"] and _bot:
        asyncio.create_task(_bot.send_message(
            f"✅ *{target}* 님의 참여가 *{actor}* 에 의해 승인되었습니다. 환영합니다!"
        ))
    return result


async def _handle_reject(db: aiosqlite.Connection, actor: str, target: str, reason: str = "") -> dict:
    result = await reject_member(db, actor, target, reason)
    if result["ok"] and _bot:
        reason_txt = f" (사유: {reason})" if reason else ""
        asyncio.create_task(_bot.send_message(
            f"❌ *{target}* 님의 참여 신청이 거절되었습니다.{reason_txt}"
        ))
    return result


async def _handle_set_role(db: aiosqlite.Connection, actor: str, target: str, new_role: str) -> dict:
    result = await set_role(db, actor, target, new_role)
    if result["ok"] and _bot:
        role_label = "관리자" if new_role == "admin" else "일반 멤버"
        asyncio.create_task(_bot.send_message(
            f"🔧 *{target}* 님이 *{role_label}*로 변경되었습니다. (by {actor})"
        ))
    return result


async def _handle_transfer(db: aiosqlite.Connection, actor: str, target: str) -> dict:
    result = await transfer_owner(db, actor, target)
    if result["ok"] and _bot:
        asyncio.create_task(_bot.send_message(
            f"👑 그룹장이 *{actor}* → *{target}* 으로 위임되었습니다."
        ))
    return result


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
        _bot.set_join_callback(lambda uid, name, cid, code: _handle_join(_db, uid, name, cid, code))
        _bot.set_leave_callback(lambda uid: _handle_leave(_db, uid))
        _bot.set_invite_callback(lambda uid: _handle_invite(_db, uid))
        _bot.set_members_callback(lambda: list_active_members(_db))
        _bot.set_kick_callback(lambda actor, target, reason: _handle_kick(_db, actor, target, reason))
        _bot.set_approve_callback(lambda actor, target: _handle_approve(_db, actor, target))
        _bot.set_reject_callback(lambda actor, target, reason: _handle_reject(_db, actor, target, reason))
        _bot.set_pending_callback(lambda: list_pending_members(_db))
        _bot.set_set_role_callback(lambda actor, target, role: _handle_set_role(_db, actor, target, role))
        _bot.set_transfer_callback(lambda actor, target: _handle_transfer(_db, actor, target))
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
# Auth API
# ---------------------------------------------------------------------------


@app.post("/auth/register")
async def auth_register(req: RegisterRequest, response: Response):
    db = await get_db()
    pw_hash = hash_password(req.password)

    # 기존 멤버 확인
    async with db.execute(
        "SELECT status, password_hash FROM members WHERE user_id = ?", (req.user_id,)
    ) as cur:
        row = await cur.fetchone()

    if row:
        status, existing_hash = row[0], row[1]
        if status == "active" and existing_hash:
            raise HTTPException(status_code=400, detail="이미 등록된 계정입니다. 로그인해주세요.")
        if status == "active" and not existing_hash:
            # 레거시 멤버: 비밀번호 설정
            await db.execute(
                "UPDATE members SET password_hash = ? WHERE user_id = ?",
                (pw_hash, req.user_id),
            )
            await db.commit()
            token = create_session_token(req.user_id)
            response.set_cookie("tf_session", token, httponly=True, samesite="lax", secure=True, max_age=SESSION_EXPIRE_DAYS * 86400)
            return {"status": "ok", "user_id": req.user_id, "message": "비밀번호가 설정되었습니다."}
        if status == "pending":
            raise HTTPException(status_code=400, detail="이미 참여 신청 중입니다. 관리자 승인을 기다려주세요.")
        # left / kicked → 재가입 처리 (아래로)

    # 초대코드 처리
    invited_by = ""
    target_status = "pending"
    if req.invite_code:
        inviter = await consume_invite(db, req.invite_code, req.user_id)
        if not inviter:
            raise HTTPException(status_code=400, detail="유효하지 않거나 이미 사용된 초대코드입니다.")
        invited_by = inviter
        target_status = "active"

    now = datetime.utcnow().isoformat()
    if row:
        # left/kicked 재가입
        await db.execute(
            """
            UPDATE members SET status = ?, role = 'member', display_name = ?,
                invited_by = ?, joined_at = ?, left_at = NULL, password_hash = ?
            WHERE user_id = ?
            """,
            (target_status, req.display_name, invited_by, now, pw_hash, req.user_id),
        )
    else:
        await db.execute(
            """
            INSERT INTO members (user_id, display_name, invited_by, role, status, joined_at, password_hash)
            VALUES (?, ?, ?, 'member', ?, ?, ?)
            """,
            (req.user_id, req.display_name, invited_by, target_status, now, pw_hash),
        )
    await db.commit()

    if target_status == "active":
        token = create_session_token(req.user_id)
        response.set_cookie("tf_session", token, httponly=True, samesite="lax", secure=True, max_age=SESSION_EXPIRE_DAYS * 86400)
        if _bot:
            asyncio.create_task(_bot.send_message(
                f"🎊 *{req.display_name or req.user_id}* 님이 Token Flex에 참여했습니다!"
            ))
        return {"status": "ok", "user_id": req.user_id}
    else:
        if _bot:
            asyncio.create_task(_bot.send_message(
                f"📋 *{req.display_name or req.user_id}* 님이 참여를 신청했습니다.\n관리자: `/approve {req.user_id}` 또는 `/reject {req.user_id}`"
            ))
        return {"status": "pending", "user_id": req.user_id, "message": "참여 신청 완료! 관리자 승인을 기다려주세요."}


@app.post("/auth/login")
async def auth_login(req: LoginRequest, response: Response):
    db = await get_db()
    async with db.execute(
        "SELECT status, password_hash FROM members WHERE user_id = ?", (req.user_id,)
    ) as cur:
        row = await cur.fetchone()

    if not row:
        raise HTTPException(status_code=401, detail="등록되지 않은 계정입니다. 회원가입해주세요.")

    status, pw_hash = row[0], row[1]

    if not pw_hash:
        raise HTTPException(status_code=401, detail="비밀번호가 설정되지 않은 계정입니다. 회원가입으로 비밀번호를 설정해주세요.")

    if not verify_password(req.password, pw_hash):
        raise HTTPException(status_code=401, detail="비밀번호가 올바르지 않습니다.")

    if status == "pending":
        raise HTTPException(status_code=403, detail="참여 신청이 아직 승인되지 않았습니다.")
    if status in ("kicked", "left"):
        raise HTTPException(status_code=403, detail="탈퇴하거나 추방된 계정입니다. 회원가입으로 재신청해주세요.")

    token = create_session_token(req.user_id)
    response.set_cookie("tf_session", token, httponly=True, samesite="lax", secure=True, max_age=SESSION_EXPIRE_DAYS * 86400)
    return {"status": "ok", "user_id": req.user_id}


@app.post("/auth/logout")
async def auth_logout(response: Response):
    response.delete_cookie("tf_session")
    return {"status": "ok"}


@app.get("/auth/me")
async def auth_me(request: Request):
    user_id = get_current_user(request)
    if not user_id:
        return {"user_id": None}
    db = await get_db()
    async with db.execute(
        "SELECT display_name, role, status FROM members WHERE user_id = ?", (user_id,)
    ) as cur:
        row = await cur.fetchone()
    if not row or row[2] != "active":
        return {"user_id": None}
    return {"user_id": user_id, "display_name": row[0], "role": row[1]}


# ---------------------------------------------------------------------------
# Member management API
# ---------------------------------------------------------------------------


@app.post("/join")
async def api_join(req: JoinRequest):
    db = await get_db()
    result = await _handle_join(db, req.user_id, req.display_name, req.telegram_chat_id, req.invite_code)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.post("/leave/{user_id}")
async def api_leave(user_id: str, request: Request, response: Response):
    db = await get_db()
    session_user = get_current_user(request)
    if session_user and session_user != user_id:
        raise HTTPException(status_code=403, detail="본인만 탈퇴할 수 있습니다.")
    result = await _handle_leave(db, user_id)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["error"])
    if session_user:
        response.delete_cookie("tf_session")
    return result


@app.post("/kick/{target}")
async def api_kick(target: str, request: Request, reason: str = ""):
    db = await get_db()
    actor = get_current_user(request)
    if not actor:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")
    result = await _handle_kick(db, actor, target, reason)
    if not result["ok"]:
        raise HTTPException(status_code=403, detail=result["error"])
    return result


@app.post("/approve/{target}")
async def api_approve(target: str, request: Request):
    db = await get_db()
    actor = get_current_user(request)
    if not actor:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")
    result = await _handle_approve(db, actor, target)
    if not result["ok"]:
        raise HTTPException(status_code=403, detail=result["error"])
    return result


@app.post("/reject/{target}")
async def api_reject(target: str, request: Request, reason: str = ""):
    db = await get_db()
    actor = get_current_user(request)
    if not actor:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")
    result = await _handle_reject(db, actor, target, reason)
    if not result["ok"]:
        raise HTTPException(status_code=403, detail=result["error"])
    return result


@app.post("/set-role/{target}")
async def api_set_role(target: str, request: Request, role: str = ""):
    db = await get_db()
    actor = get_current_user(request)
    if not actor:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")
    if not role:
        raise HTTPException(status_code=400, detail="role parameter required")
    result = await _handle_set_role(db, actor, target, role)
    if not result["ok"]:
        raise HTTPException(status_code=403, detail=result["error"])
    return result


@app.post("/transfer-owner/{target}")
async def api_transfer(target: str, request: Request):
    db = await get_db()
    actor = get_current_user(request)
    if not actor:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")
    result = await _handle_transfer(db, actor, target)
    if not result["ok"]:
        raise HTTPException(status_code=403, detail=result["error"])
    return result


@app.get("/members")
async def api_members():
    db = await get_db()
    members = await list_active_members(db)
    return {"members": members, "count": len(members)}


@app.get("/pending")
async def api_pending(request: Request):
    db = await get_db()
    actor = get_current_user(request)
    if not actor or not await is_admin_or_owner(db, actor):
        raise HTTPException(status_code=403, detail="관리자만 조회할 수 있습니다.")
    pending = await list_pending_members(db)
    return {"pending": pending, "count": len(pending)}


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
    if not await is_active_member(db, payload.user_id):
        raise HTTPException(status_code=403, detail=f"'{payload.user_id}'는 활성 멤버가 아닙니다. /join으로 먼저 참여하세요.")

    async with db.execute("SELECT used_tokens FROM usage WHERE user_id = ?", (payload.user_id,)) as cur:
        prev_row = await cur.fetchone()
        prev_tokens = prev_row[0] if prev_row else 0

    await db.execute(
        "INSERT INTO usage (user_id, used_tokens, last_reported) VALUES (:uid, :tokens, :ts) ON CONFLICT(user_id) DO UPDATE SET used_tokens = :tokens, last_reported = :ts",
        {"uid": payload.user_id, "tokens": payload.used_tokens, "ts": payload.reported_at.isoformat()},
    )

    alerts: list[AlertEvent] = []
    alerts.extend(await check_milestones(db, payload.user_id, payload.used_tokens))
    da = await check_daily_limit(db, payload.user_id, payload.used_tokens)
    if da:
        alerts.append(da)
    if payload.used_tokens > prev_tokens:
        ra = await check_rank_change(db, payload.user_id, payload.used_tokens)
        if ra:
            alerts.append(ra)

    await db.commit()
    if _bot and alerts:
        for a in alerts:
            asyncio.create_task(_bot.broadcast_alert(a))

    async with db.execute("SELECT COUNT(*) FROM usage WHERE used_tokens > ?", (payload.used_tokens,)) as cur:
        rank = (await cur.fetchone())[0] + 1
    async with db.execute("SELECT COUNT(*) FROM members WHERE status = 'active'") as cur:
        total = (await cur.fetchone())[0]

    return UsageResponse(status="ok", rank=rank, total_users=total)


@app.get("/rank", response_model=RankBoard)
async def get_rank():
    return await build_rank_board()


@app.get("/stats")
async def stats():
    return await get_global_stats(await get_db())


@app.get("/user/{user_id}")
async def user_detail(user_id: str):
    result = await get_user_stats(await get_db(), user_id)
    if not result:
        raise HTTPException(status_code=404, detail="User not found")
    return result


@app.get("/daily/{user_id}")
async def daily_history(user_id: str, limit: int = 30):
    return await get_daily_history(await get_db(), user_id, min(limit, 90))


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
