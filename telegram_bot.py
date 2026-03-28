"""Token Flex Dashboard — Telegram bot module.

Commands (일반):
  /rank, /me, /today, /stats, /milestone, /daily
  /join, /leave, /invite, /members, /help

Commands (관리자):
  /approve <id>       — 참여 신청 승인
  /reject <id> [사유]  — 참여 신청 거절
  /kick <id> [사유]    — 멤버 추방
  /pending            — 대기 중인 신청 목록
  /setadmin <id>      — 관리자 임명
  /setmember <id>     — 일반 멤버로 강등
  /transfer <id>      — 그룹장 위임 (owner만)
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Awaitable, Callable

import httpx

if TYPE_CHECKING:
    from schemas import AlertEvent, RankBoard

logger = logging.getLogger("tokenflex.telegram")


class TelegramBot:
    BASE = "https://api.telegram.org/bot{token}"

    def __init__(self, token: str, chat_id: str) -> None:
        self.token = token
        self.chat_id = chat_id
        self._base = self.BASE.format(token=token)
        self._client: httpx.AsyncClient | None = None
        self._offset: int = 0
        self._running = False

        # 콜백 슬롯
        self._rank_cb = None
        self._stats_cb = None
        self._user_cb = None
        self._daily_cb = None
        self._join_cb = None
        self._leave_cb = None
        self._invite_cb = None
        self._members_cb = None
        self._kick_cb = None
        self._approve_cb = None
        self._reject_cb = None
        self._pending_cb = None
        self._set_role_cb = None
        self._transfer_cb = None

    # -- lifecycle --
    async def start(self):
        self._client = httpx.AsyncClient(timeout=30.0)
        self._running = True
        logger.info("Telegram bot polling started (chat_id=%s)", self.chat_id)

    async def stop(self):
        self._running = False
        if self._client:
            await self._client.aclose()
            self._client = None

    # -- callback setters --
    def set_rank_callback(self, cb): self._rank_cb = cb
    def set_stats_callback(self, cb): self._stats_cb = cb
    def set_user_callback(self, cb): self._user_cb = cb
    def set_daily_callback(self, cb): self._daily_cb = cb
    def set_join_callback(self, cb): self._join_cb = cb
    def set_leave_callback(self, cb): self._leave_cb = cb
    def set_invite_callback(self, cb): self._invite_cb = cb
    def set_members_callback(self, cb): self._members_cb = cb
    def set_kick_callback(self, cb): self._kick_cb = cb
    def set_approve_callback(self, cb): self._approve_cb = cb
    def set_reject_callback(self, cb): self._reject_cb = cb
    def set_pending_callback(self, cb): self._pending_cb = cb
    def set_set_role_callback(self, cb): self._set_role_cb = cb
    def set_transfer_callback(self, cb): self._transfer_cb = cb

    # -- messaging --
    async def send_message(self, text: str, chat_id: str | None = None):
        if not self._client:
            return
        target = chat_id or self.chat_id
        try:
            resp = await self._client.post(
                f"{self._base}/sendMessage",
                json={"chat_id": target, "text": text, "parse_mode": "Markdown"},
            )
            if resp.status_code != 200:
                logger.warning("sendMessage failed: %s", resp.text)
        except Exception as exc:
            logger.warning("sendMessage error: %s", exc)

    async def broadcast_alert(self, event: AlertEvent):
        await self.send_message(event.message)

    # -- formatters --
    @staticmethod
    def _fmt(n: int) -> str:
        if n >= 1_000_000_000: return f"{n/1e9:.1f}B"
        if n >= 1_000_000: return f"{n/1e6:.1f}M"
        if n >= 1_000: return f"{n/1e3:.1f}K"
        return str(n)

    @staticmethod
    def _bar(v: int, mx: int, w: int = 12) -> str:
        if mx <= 0: return "░" * w
        f = max(1, round(v / mx * w))
        return "▓" * f + "░" * (w - f)

    @staticmethod
    def _role_badge(role: str) -> str:
        return {"owner": "👑", "admin": "⚙️"}.get(role, "")

    @staticmethod
    def format_rank_board(board: RankBoard) -> str:
        if not board.board:
            return "📊 *Token Flex Ranking*\n\n등록된 유저가 없습니다."
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        mx = board.board[0].used_tokens if board.board else 1
        lines = ["🏆 *Token Flex Ranking*", ""]
        for e in board.board:
            medal = medals.get(e.rank, f"`{e.rank:>2}.`")
            bar = TelegramBot._bar(e.used_tokens, mx)
            lines.append(f"{medal} *{e.user_id}*")
            lines.append(f"    {bar} `{e.used_tokens:>12,}` tok")
        lines.append("")
        lines.append(f"_Updated: {board.generated_at.strftime('%Y-%m-%d %H:%M UTC')}_")
        return "\n".join(lines)

    # -- polling --
    async def poll_loop(self):
        while self._running:
            try:
                resp = await self._client.get(
                    f"{self._base}/getUpdates",
                    params={"offset": self._offset, "timeout": 20},
                    timeout=30.0,
                )
                if resp.status_code != 200:
                    await asyncio.sleep(5)
                    continue
                for upd in resp.json().get("result", []):
                    self._offset = upd["update_id"] + 1
                    await self._handle(upd)
            except httpx.ReadTimeout:
                continue
            except Exception as exc:
                logger.warning("poll error: %s", exc)
                await asyncio.sleep(5)

    async def _handle(self, upd: dict):
        msg = upd.get("message", {})
        text = (msg.get("text") or "").strip()
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if not text.startswith("/"):
            return

        parts = text.split()
        cmd = parts[0].lower().split("@")[0]
        args = parts[1:]

        dispatch = {
            # 일반
            "/rank": self._cmd_rank,
            "/me": self._cmd_me,
            "/today": self._cmd_today,
            "/stats": self._cmd_stats,
            "/milestone": self._cmd_milestone,
            "/daily": self._cmd_daily,
            "/join": self._cmd_join,
            "/leave": self._cmd_leave,
            "/invite": self._cmd_invite,
            "/members": self._cmd_members,
            # 관리자
            "/approve": self._cmd_approve,
            "/reject": self._cmd_reject,
            "/kick": self._cmd_kick,
            "/pending": self._cmd_pending,
            "/setadmin": self._cmd_setadmin,
            "/setmember": self._cmd_setmember,
            "/transfer": self._cmd_transfer,
            # 기타
            "/help": self._cmd_help,
            "/start": self._cmd_help,
        }
        handler = dispatch.get(cmd)
        if handler:
            await handler(chat_id, args, msg)

    # ======================================================================
    # 일반 명령어
    # ======================================================================

    async def _cmd_rank(self, cid, args, msg):
        if not self._rank_cb:
            return await self.send_message("데이터를 불러올 수 없습니다.", cid)
        await self.send_message(self.format_rank_board(await self._rank_cb()), cid)

    async def _cmd_me(self, cid, args, msg):
        if not args:
            return await self.send_message("사용법: `/me <user_id>`", cid)
        if not self._user_cb:
            return await self.send_message("데이터를 불러올 수 없습니다.", cid)
        d = await self._user_cb(args[0])
        if not d:
            return await self.send_message(f"`{args[0]}` 유저를 찾을 수 없습니다.", cid)

        ms = ""
        if d["milestones"]:
            ms = "\n🏅 마일스톤: " + ", ".join(f"`{m['milestone']:,}`" for m in d["milestones"])
        inv = f"\n🎟️ 초대자: *{d['invited_by']}*" if d.get("invited_by") else ""
        role_badge = self._role_badge(d.get("role", ""))

        await self.send_message(
            f"👤 *{d['user_id']}* {role_badge}\n\n"
            f"🏆 순위: *#{d['rank']}* / {d['total_users']}\n"
            f"📊 누적: `{d['used_tokens']:,}` tokens\n"
            f"📅 오늘: `{d['today_tokens']:,}` tokens\n"
            f"🕐 마지막 보고: _{d['last_reported']}_"
            f"{ms}{inv}", cid
        )

    async def _cmd_today(self, cid, args, msg):
        if not self._stats_cb:
            return await self.send_message("데이터를 불러올 수 없습니다.", cid)
        s = await self._stats_cb()
        tl = s.get("today_leader")
        if not tl:
            return await self.send_message("📅 오늘은 아직 보고된 데이터가 없습니다.", cid)
        await self.send_message(
            f"📅 *오늘의 Token Flex*\n\n"
            f"🥇 리더: *{tl['user_id']}* — `{tl['tokens']:,}` tokens\n"
            f"👥 참여자: {s['total_users']}명\n"
            f"📊 전체 누적: `{s['total_tokens']:,}` tokens", cid
        )

    async def _cmd_stats(self, cid, args, msg):
        if not self._stats_cb:
            return await self.send_message("데이터를 불러올 수 없습니다.", cid)
        s = await self._stats_cb()
        leader = f"👑 1위: *{s['leader']['user_id']}* (`{s['leader']['used_tokens']:,}` tok)\n" if s.get("leader") else ""
        ms = f"🏅 최근 마일스톤: *{s['latest_milestone']['user_id']}* — `{s['latest_milestone']['milestone']:,}`\n" if s.get("latest_milestone") else ""
        pending = f"📋 승인 대기: *{s['pending_count']}*건\n" if s.get("pending_count") else ""
        await self.send_message(
            f"📈 *Global Stats*\n\n"
            f"👥 활성 멤버: *{s['total_users']}*명\n"
            f"🔥 전체 토큰: `{s['total_tokens']:,}`\n"
            f"{leader}{ms}{pending}"
            f"⚠️ 일일 한도: `{s['daily_limit']:,}`", cid
        )

    async def _cmd_milestone(self, cid, args, msg):
        if not self._stats_cb:
            return await self.send_message("데이터를 불러올 수 없습니다.", cid)
        ms = (await self._stats_cb()).get("latest_milestone")
        if not ms:
            return await self.send_message("아직 달성된 마일스톤이 없습니다.", cid)
        await self.send_message(
            f"🏅 *Latest Milestone*\n\n"
            f"👤 *{ms['user_id']}*\n"
            f"🎯 `{ms['milestone']:,}` tokens\n"
            f"🕐 _{ms['reached_at']}_", cid
        )

    async def _cmd_daily(self, cid, args, msg):
        if not args:
            return await self.send_message("사용법: `/daily <user_id>`", cid)
        if not self._daily_cb:
            return await self.send_message("데이터를 불러올 수 없습니다.", cid)
        h = await self._daily_cb(args[0])
        if not h:
            return await self.send_message(f"`{args[0]}` 의 일별 기록이 없습니다.", cid)
        mx = max(d["tokens"] for d in h)
        lines = [f"📊 *{args[0]}* — 최근 {len(h)}일 추이", ""]
        for d in h:
            lines.append(f"`{d['date'][5:]}` {self._bar(d['tokens'], mx, 10)} `{self._fmt(d['tokens'])}`")
        await self.send_message("\n".join(lines), cid)

    # -- join / leave / invite / members --

    async def _cmd_join(self, cid, args, msg):
        if not args:
            return await self.send_message(
                "사용법:\n"
                "• `/join <user_id>` — 참여 신청 (관리자 승인 필요)\n"
                "• `/join <user_id> <초대코드>` — 즉시 참여", cid
            )
        if not self._join_cb:
            return await self.send_message("가입 기능을 사용할 수 없습니다.", cid)

        uid = args[0]
        code = args[1] if len(args) > 1 else ""
        fr = msg.get("from", {})
        name = (fr.get("first_name", "") + " " + fr.get("last_name", "")).strip() or uid

        result = await self._join_cb(uid, name, cid, code)
        if result.get("ok"):
            status = result.get("status", "active")
            if status == "pending":
                await self.send_message(
                    f"📋 *{name}* (`{uid}`) 참여 신청 완료!\n관리자 승인을 기다려주세요.", cid
                )
            else:
                inv = f"\n🎟️ 초대자: *{result['invited_by']}*" if result.get("invited_by") else ""
                await self.send_message(
                    f"✅ *{name}* (`{uid}`) 참여 완료!{inv}\n\n"
                    f"클라이언트: `TOKENFLEX_USER_ID={uid}`", cid
                )
        else:
            await self.send_message(f"❌ {result.get('error', '가입 실패')}", cid)

    async def _cmd_leave(self, cid, args, msg):
        if not args:
            return await self.send_message("사용법: `/leave <user_id>`", cid)
        if not self._leave_cb:
            return await self.send_message("탈퇴 기능을 사용할 수 없습니다.", cid)
        result = await self._leave_cb(args[0])
        if result.get("ok"):
            await self.send_message(f"👋 `{args[0]}` 님이 탈퇴했습니다. 다시 돌아오세요!", cid)
        else:
            await self.send_message(f"❌ {result.get('error')}", cid)

    async def _cmd_invite(self, cid, args, msg):
        if not args:
            return await self.send_message("사용법: `/invite <your_user_id>`", cid)
        if not self._invite_cb:
            return await self.send_message("초대 기능을 사용할 수 없습니다.", cid)
        result = await self._invite_cb(args[0])
        if result.get("ok"):
            await self.send_message(
                f"🎟️ *초대코드 생성!*\n\n"
                f"코드: `{result['code']}`\n\n"
                f"친구에게 전달 → `/join <id> {result['code']}`", cid
            )
        else:
            await self.send_message(f"❌ {result.get('error')}", cid)

    async def _cmd_members(self, cid, args, msg):
        if not self._members_cb:
            return await self.send_message("데이터를 불러올 수 없습니다.", cid)
        members = await self._members_cb()
        if not members:
            return await self.send_message("등록된 멤버가 없습니다.", cid)

        lines = [f"👥 *활성 멤버* ({len(members)}명)", ""]
        for i, m in enumerate(members, 1):
            name = m["display_name"] or m["user_id"]
            badge = self._role_badge(m["role"])
            inv = f" ← {m['invited_by']}" if m["invited_by"] else ""
            lines.append(f"`{i:>2}.` {badge} *{name}* (`{m['user_id']}`){inv}")
        await self.send_message("\n".join(lines), cid)

    # ======================================================================
    # 관리자 전용 명령어
    # ======================================================================

    async def _cmd_approve(self, cid, args, msg):
        if not args:
            return await self.send_message("사용법: `/approve <user_id>`\n관리자만 사용 가능", cid)
        if not self._approve_cb:
            return await self.send_message("기능을 사용할 수 없습니다.", cid)
        # actor = 명령을 보낸 사람의 user_id를 추적할 수 없으므로 args[0] 뒤에 actor를 두 번째로 받거나
        # 여기서는 from 유저 정보 기반으로 처리
        actor = self._resolve_actor(args, msg)
        target = args[0]
        result = await self._approve_cb(actor, target)
        if not result.get("ok"):
            await self.send_message(f"❌ {result.get('error')}", cid)

    async def _cmd_reject(self, cid, args, msg):
        if not args:
            return await self.send_message("사용법: `/reject <user_id> [사유]`\n관리자만 사용 가능", cid)
        if not self._reject_cb:
            return await self.send_message("기능을 사용할 수 없습니다.", cid)
        actor = self._resolve_actor(args, msg)
        target = args[0]
        reason = " ".join(args[1:]) if len(args) > 1 else ""
        result = await self._reject_cb(actor, target, reason)
        if not result.get("ok"):
            await self.send_message(f"❌ {result.get('error')}", cid)

    async def _cmd_kick(self, cid, args, msg):
        if not args:
            return await self.send_message("사용법: `/kick <user_id> [사유]`\n관리자만 사용 가능", cid)
        if not self._kick_cb:
            return await self.send_message("기능을 사용할 수 없습니다.", cid)
        actor = self._resolve_actor(args, msg)
        target = args[0]
        reason = " ".join(args[1:]) if len(args) > 1 else ""
        result = await self._kick_cb(actor, target, reason)
        if not result.get("ok"):
            await self.send_message(f"❌ {result.get('error')}", cid)

    async def _cmd_pending(self, cid, args, msg):
        if not self._pending_cb:
            return await self.send_message("기능을 사용할 수 없습니다.", cid)
        pending = await self._pending_cb()
        if not pending:
            return await self.send_message("📋 대기 중인 참여 신청이 없습니다.", cid)
        lines = [f"📋 *참여 신청 대기* ({len(pending)}건)", ""]
        for p in pending:
            name = p["display_name"] or p["user_id"]
            inv = f" (추천: {p['invited_by']})" if p.get("invited_by") else ""
            lines.append(f"• *{name}* (`{p['user_id']}`){inv}")
            lines.append(f"  신청: _{p['requested_at']}_")
        lines.append("")
        lines.append("`/approve <id>` 또는 `/reject <id> [사유]`")
        await self.send_message("\n".join(lines), cid)

    async def _cmd_setadmin(self, cid, args, msg):
        if not args:
            return await self.send_message("사용법: `/setadmin <user_id>`\n그룹장만 사용 가능", cid)
        if not self._set_role_cb:
            return await self.send_message("기능을 사용할 수 없습니다.", cid)
        actor = self._resolve_actor(args, msg)
        result = await self._set_role_cb(actor, args[0], "admin")
        if result.get("ok"):
            await self.send_message(f"⚙️ *{args[0]}* 님이 관리자로 임명되었습니다.", cid)
        else:
            await self.send_message(f"❌ {result.get('error')}", cid)

    async def _cmd_setmember(self, cid, args, msg):
        if not args:
            return await self.send_message("사용법: `/setmember <user_id>`\n그룹장만 사용 가능", cid)
        if not self._set_role_cb:
            return await self.send_message("기능을 사용할 수 없습니다.", cid)
        actor = self._resolve_actor(args, msg)
        result = await self._set_role_cb(actor, args[0], "member")
        if result.get("ok"):
            await self.send_message(f"👤 *{args[0]}* 님이 일반 멤버로 변경되었습니다.", cid)
        else:
            await self.send_message(f"❌ {result.get('error')}", cid)

    async def _cmd_transfer(self, cid, args, msg):
        if not args:
            return await self.send_message("사용법: `/transfer <user_id>`\n현재 그룹장만 사용 가능", cid)
        if not self._transfer_cb:
            return await self.send_message("기능을 사용할 수 없습니다.", cid)
        actor = self._resolve_actor(args, msg)
        result = await self._transfer_cb(actor, args[0])
        if not result.get("ok"):
            await self.send_message(f"❌ {result.get('error')}", cid)

    # ======================================================================
    # /help
    # ======================================================================

    async def _cmd_help(self, cid, args, msg):
        await self.send_message(
            "🤖 *Token Flex Bot*\n"
            "\n"
            "*📊 조회*\n"
            "`/rank` — 전체 랭킹 보드\n"
            "`/me <id>` — 개인 상세 통계\n"
            "`/today` — 오늘의 리더보드\n"
            "`/stats` — 글로벌 집계 요약\n"
            "`/milestone` — 최근 마일스톤\n"
            "`/daily <id>` — 최근 7일 추이\n"
            "\n"
            "*👥 멤버*\n"
            "`/join <id> [코드]` — 참여 (코드 없으면 신청)\n"
            "`/leave <id>` — 탈퇴\n"
            "`/invite <id>` — 초대코드 생성\n"
            "`/members` — 멤버 목록\n"
            "\n"
            "*🔧 관리자*\n"
            "`/pending` — 대기 신청 목록\n"
            "`/approve <id>` — 신청 승인\n"
            "`/reject <id> [사유]` — 신청 거절\n"
            "`/kick <id> [사유]` — 추방\n"
            "`/setadmin <id>` — 관리자 임명\n"
            "`/setmember <id>` — 일반 멤버로\n"
            "`/transfer <id>` — 그룹장 위임\n"
            "\n"
            "`/help` — 이 도움말", cid
        )

    # ======================================================================
    # Helpers
    # ======================================================================

    @staticmethod
    def _resolve_actor(args: list[str], msg: dict) -> str:
        """텔레그램 username을 actor로 사용. 없으면 user_id 숫자."""
        fr = msg.get("from", {})
        return fr.get("username") or str(fr.get("id", "unknown"))
