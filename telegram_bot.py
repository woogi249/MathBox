"""Token Flex Dashboard — Telegram bot module.

경량 구현: python-telegram-bot 의존 없이 httpx로 Bot API를 직접 호출한다.
서버 프로세스 내부에서 polling loop로 동작하며,
알림 브로드캐스트와 명령 응답을 담당한다.

Commands:
  /rank        — 전체 랭킹 보드
  /me <id>     — 개인 상세 통계
  /today       — 오늘의 사용량 리더보드
  /stats       — 글로벌 집계 요약
  /milestone   — 최근 마일스톤 기록
  /daily <id>  — 최근 7일 사용량 추이
  /join <id> [code]  — 그룹 참여 (초대코드 필요)
  /leave <id>  — 그룹 탈퇴
  /invite      — 초대코드 생성 (멤버만)
  /members     — 현재 활성 멤버 목록
  /help        — 명령어 목록
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Awaitable, Callable

import httpx

if TYPE_CHECKING:
    from schemas import AlertEvent, RankBoard

logger = logging.getLogger("tokenflex.telegram")


class TelegramBot:
    """최소 Telegram Bot API 클라이언트."""

    BASE = "https://api.telegram.org/bot{token}"

    def __init__(self, token: str, chat_id: str) -> None:
        self.token = token
        self.chat_id = chat_id
        self._base = self.BASE.format(token=token)
        self._client: httpx.AsyncClient | None = None
        self._offset: int = 0
        self._running = False

        # 서버에서 주입하는 콜백들
        self._rank_cb: Callable[[], Awaitable[RankBoard]] | None = None
        self._stats_cb: Callable[[], Awaitable[dict]] | None = None
        self._user_cb: Callable[[str], Awaitable[dict | None]] | None = None
        self._daily_cb: Callable[[str], Awaitable[list[dict]]] | None = None
        self._join_cb: Callable[..., Awaitable[dict]] | None = None
        self._leave_cb: Callable[[str], Awaitable[dict]] | None = None
        self._invite_cb: Callable[[str], Awaitable[dict]] | None = None
        self._members_cb: Callable[[], Awaitable[list[dict]]] | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._client = httpx.AsyncClient(timeout=30.0)
        self._running = True
        logger.info("Telegram bot polling started (chat_id=%s)", self.chat_id)

    async def stop(self) -> None:
        self._running = False
        if self._client:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # Callback setters
    # ------------------------------------------------------------------

    def set_rank_callback(self, cb): self._rank_cb = cb
    def set_stats_callback(self, cb): self._stats_cb = cb
    def set_user_callback(self, cb): self._user_cb = cb
    def set_daily_callback(self, cb): self._daily_cb = cb
    def set_join_callback(self, cb): self._join_cb = cb
    def set_leave_callback(self, cb): self._leave_cb = cb
    def set_invite_callback(self, cb): self._invite_cb = cb
    def set_members_callback(self, cb): self._members_cb = cb

    # ------------------------------------------------------------------
    # Message sending
    # ------------------------------------------------------------------

    async def send_message(self, text: str, chat_id: str | None = None) -> None:
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

    async def broadcast_alert(self, event: AlertEvent) -> None:
        await self.send_message(event.message)

    # ------------------------------------------------------------------
    # Formatters
    # ------------------------------------------------------------------

    @staticmethod
    def _fmt_tokens(n: int) -> str:
        if n >= 1_000_000_000:
            return f"{n / 1_000_000_000:.1f}B"
        if n >= 1_000_000:
            return f"{n / 1_000_000:.1f}M"
        if n >= 1_000:
            return f"{n / 1_000:.1f}K"
        return str(n)

    @staticmethod
    def _bar(value: int, max_val: int, width: int = 12) -> str:
        if max_val <= 0:
            return "░" * width
        filled = max(1, round(value / max_val * width))
        return "▓" * filled + "░" * (width - filled)

    @staticmethod
    def format_rank_board(board: RankBoard) -> str:
        if not board.board:
            return "📊 *Token Flex Ranking*\n\n등록된 유저가 없습니다."

        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        max_tok = board.board[0].used_tokens if board.board else 1
        lines = ["🏆 *Token Flex Ranking*", ""]

        for e in board.board:
            medal = medals.get(e.rank, f"`{e.rank:>2}.`")
            bar = TelegramBot._bar(e.used_tokens, max_tok)
            lines.append(f"{medal} *{e.user_id}*")
            lines.append(f"    {bar} `{e.used_tokens:>12,}` tok")

        lines.append("")
        lines.append(f"_Updated: {board.generated_at.strftime('%Y-%m-%d %H:%M UTC')}_")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Polling loop
    # ------------------------------------------------------------------

    async def poll_loop(self) -> None:
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
                data = resp.json()
                for update in data.get("result", []):
                    self._offset = update["update_id"] + 1
                    await self._handle_update(update)
            except httpx.ReadTimeout:
                continue
            except Exception as exc:
                logger.warning("poll_loop error: %s", exc)
                await asyncio.sleep(5)

    async def _handle_update(self, update: dict) -> None:
        msg = update.get("message", {})
        text = (msg.get("text") or "").strip()
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if not text.startswith("/"):
            return

        parts = text.split()
        cmd = parts[0].lower().split("@")[0]
        args = parts[1:]

        dispatch = {
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
            "/help": self._cmd_help,
            "/start": self._cmd_help,
        }

        handler = dispatch.get(cmd)
        if handler:
            await handler(chat_id, args, msg)

    # ------------------------------------------------------------------
    # /rank
    # ------------------------------------------------------------------

    async def _cmd_rank(self, chat_id: str, args: list[str], msg: dict) -> None:
        if not self._rank_cb:
            await self.send_message("데이터를 불러올 수 없습니다.", chat_id)
            return
        board = await self._rank_cb()
        await self.send_message(self.format_rank_board(board), chat_id)

    # ------------------------------------------------------------------
    # /me <user_id>
    # ------------------------------------------------------------------

    async def _cmd_me(self, chat_id: str, args: list[str], msg: dict) -> None:
        if not args:
            await self.send_message("사용법: `/me <user_id>`", chat_id)
            return
        if not self._user_cb:
            await self.send_message("데이터를 불러올 수 없습니다.", chat_id)
            return

        data = await self._user_cb(args[0])
        if not data:
            await self.send_message(f"`{args[0]}` 유저를 찾을 수 없습니다.", chat_id)
            return

        ms_text = ""
        if data["milestones"]:
            ms_list = [f"`{m['milestone']:,}`" for m in data["milestones"]]
            ms_text = f"\n🏅 달성 마일스톤: {', '.join(ms_list)}"

        invite_text = f"\n🎟️ 초대자: *{data['invited_by']}*" if data.get("invited_by") else ""

        text = (
            f"👤 *{data['user_id']}*\n"
            f"\n"
            f"🏆 순위: *#{data['rank']}* / {data['total_users']}\n"
            f"📊 누적: `{data['used_tokens']:,}` tokens\n"
            f"📅 오늘: `{data['today_tokens']:,}` tokens\n"
            f"🕐 마지막 보고: _{data['last_reported']}_"
            f"{ms_text}"
            f"{invite_text}"
        )
        await self.send_message(text, chat_id)

    # ------------------------------------------------------------------
    # /today
    # ------------------------------------------------------------------

    async def _cmd_today(self, chat_id: str, args: list[str], msg: dict) -> None:
        if not self._stats_cb:
            await self.send_message("데이터를 불러올 수 없습니다.", chat_id)
            return

        stats = await self._stats_cb()
        tl = stats.get("today_leader")
        if not tl:
            await self.send_message("📅 오늘은 아직 보고된 데이터가 없습니다.", chat_id)
            return

        text = (
            f"📅 *오늘의 Token Flex*\n"
            f"\n"
            f"🥇 리더: *{tl['user_id']}* — `{tl['tokens']:,}` tokens\n"
            f"👥 참여자: {stats['total_users']}명\n"
            f"📊 전체 누적: `{stats['total_tokens']:,}` tokens"
        )
        await self.send_message(text, chat_id)

    # ------------------------------------------------------------------
    # /stats
    # ------------------------------------------------------------------

    async def _cmd_stats(self, chat_id: str, args: list[str], msg: dict) -> None:
        if not self._stats_cb:
            await self.send_message("데이터를 불러올 수 없습니다.", chat_id)
            return

        s = await self._stats_cb()
        leader_line = ""
        if s.get("leader"):
            leader_line = f"👑 1위: *{s['leader']['user_id']}* (`{s['leader']['used_tokens']:,}` tok)\n"

        ms_line = ""
        if s.get("latest_milestone"):
            ms = s["latest_milestone"]
            ms_line = f"🏅 최근 마일스톤: *{ms['user_id']}* — `{ms['milestone']:,}`\n"

        text = (
            f"📈 *Global Stats*\n"
            f"\n"
            f"👥 활성 멤버: *{s['total_users']}*명\n"
            f"🔥 전체 토큰: `{s['total_tokens']:,}`\n"
            f"{leader_line}"
            f"{ms_line}"
            f"⚠️ 일일 한도: `{s['daily_limit']:,}`"
        )
        await self.send_message(text, chat_id)

    # ------------------------------------------------------------------
    # /milestone
    # ------------------------------------------------------------------

    async def _cmd_milestone(self, chat_id: str, args: list[str], msg: dict) -> None:
        if not self._stats_cb:
            await self.send_message("데이터를 불러올 수 없습니다.", chat_id)
            return

        s = await self._stats_cb()
        ms = s.get("latest_milestone")
        if not ms:
            await self.send_message("아직 달성된 마일스톤이 없습니다.", chat_id)
            return
        text = (
            f"🏅 *Latest Milestone*\n"
            f"\n"
            f"👤 *{ms['user_id']}*\n"
            f"🎯 `{ms['milestone']:,}` tokens\n"
            f"🕐 _{ms['reached_at']}_"
        )
        await self.send_message(text, chat_id)

    # ------------------------------------------------------------------
    # /daily <user_id>
    # ------------------------------------------------------------------

    async def _cmd_daily(self, chat_id: str, args: list[str], msg: dict) -> None:
        if not args:
            await self.send_message("사용법: `/daily <user_id>`", chat_id)
            return
        if not self._daily_cb:
            await self.send_message("데이터를 불러올 수 없습니다.", chat_id)
            return

        history = await self._daily_cb(args[0])
        if not history:
            await self.send_message(f"`{args[0]}` 의 일별 기록이 없습니다.", chat_id)
            return

        max_tok = max(d["tokens"] for d in history) if history else 1
        lines = [f"📊 *{args[0]}* — 최근 {len(history)}일 추이", ""]
        for d in history:
            bar = self._bar(d["tokens"], max_tok, 10)
            lines.append(f"`{d['date'][5:]}` {bar} `{self._fmt_tokens(d['tokens'])}`")

        await self.send_message("\n".join(lines), chat_id)

    # ------------------------------------------------------------------
    # /join <user_id> [invite_code]
    # ------------------------------------------------------------------

    async def _cmd_join(self, chat_id: str, args: list[str], msg: dict) -> None:
        if not args:
            await self.send_message(
                "사용법: `/join <user_id> [초대코드]`\n"
                "예: `/join alice abc123`",
                chat_id,
            )
            return
        if not self._join_cb:
            await self.send_message("가입 기능을 사용할 수 없습니다.", chat_id)
            return

        user_id = args[0]
        invite_code = args[1] if len(args) > 1 else ""

        # 텔레그램 유저 정보로 display_name 추출
        from_user = msg.get("from", {})
        display_name = (
            from_user.get("first_name", "")
            + (" " + from_user.get("last_name", "")).rstrip()
        ).strip() or user_id

        result = await self._join_cb(user_id, display_name, chat_id, invite_code)

        if result.get("ok"):
            invited_line = f"\n🎟️ 초대자: *{result['invited_by']}*" if result.get("invited_by") else ""
            await self.send_message(
                f"✅ *{display_name}* (`{user_id}`) 참여 완료!{invited_line}\n\n"
                f"이제 클라이언트 데몬에서 `TOKENFLEX_USER_ID={user_id}`로 설정하고 실행하세요.",
                chat_id,
            )
        else:
            await self.send_message(f"❌ {result.get('error', '가입 실패')}", chat_id)

    # ------------------------------------------------------------------
    # /leave <user_id>
    # ------------------------------------------------------------------

    async def _cmd_leave(self, chat_id: str, args: list[str], msg: dict) -> None:
        if not args:
            await self.send_message("사용법: `/leave <user_id>`", chat_id)
            return
        if not self._leave_cb:
            await self.send_message("탈퇴 기능을 사용할 수 없습니다.", chat_id)
            return

        result = await self._leave_cb(args[0])
        if result.get("ok"):
            await self.send_message(f"👋 `{args[0]}` 님이 탈퇴했습니다. 다시 돌아오세요!", chat_id)
        else:
            await self.send_message(f"❌ {result.get('error', '탈퇴 실패')}", chat_id)

    # ------------------------------------------------------------------
    # /invite — 초대코드 생성
    # ------------------------------------------------------------------

    async def _cmd_invite(self, chat_id: str, args: list[str], msg: dict) -> None:
        # 인자로 user_id를 받거나, 없으면 안내
        if not args:
            await self.send_message(
                "사용법: `/invite <your_user_id>`\n"
                "본인의 user\\_id를 입력하면 초대코드가 생성됩니다.",
                chat_id,
            )
            return
        if not self._invite_cb:
            await self.send_message("초대 기능을 사용할 수 없습니다.", chat_id)
            return

        result = await self._invite_cb(args[0])
        if result.get("ok"):
            await self.send_message(
                f"🎟️ *초대코드 생성 완료!*\n\n"
                f"코드: `{result['code']}`\n\n"
                f"친구에게 이 코드를 전달하세요.\n"
                f"친구: `/join <user_id> {result['code']}`",
                chat_id,
            )
        else:
            await self.send_message(f"❌ {result.get('error', '초대코드 생성 실패')}", chat_id)

    # ------------------------------------------------------------------
    # /members — 활성 멤버 목록
    # ------------------------------------------------------------------

    async def _cmd_members(self, chat_id: str, args: list[str], msg: dict) -> None:
        if not self._members_cb:
            await self.send_message("데이터를 불러올 수 없습니다.", chat_id)
            return

        members = await self._members_cb()
        if not members:
            await self.send_message("등록된 멤버가 없습니다.", chat_id)
            return

        lines = [f"👥 *활성 멤버* ({len(members)}명)", ""]
        for i, m in enumerate(members, 1):
            name = m["display_name"] or m["user_id"]
            invite_tag = f" ← {m['invited_by']}" if m["invited_by"] else ""
            lines.append(f"`{i:>2}.` *{name}* (`{m['user_id']}`){invite_tag}")

        await self.send_message("\n".join(lines), chat_id)

    # ------------------------------------------------------------------
    # /help
    # ------------------------------------------------------------------

    async def _cmd_help(self, chat_id: str, args: list[str], msg: dict) -> None:
        text = (
            "🤖 *Token Flex Bot*\n"
            "\n"
            "*📊 조회*\n"
            "`/rank` — 전체 랭킹 보드\n"
            "`/me <id>` — 개인 상세 통계\n"
            "`/today` — 오늘의 리더보드\n"
            "`/stats` — 글로벌 집계 요약\n"
            "`/milestone` — 최근 마일스톤 기록\n"
            "`/daily <id>` — 최근 7일 사용량 추이\n"
            "\n"
            "*👥 멤버 관리*\n"
            "`/join <id> [코드]` — 그룹 참여\n"
            "`/leave <id>` — 그룹 탈퇴\n"
            "`/invite <id>` — 초대코드 생성\n"
            "`/members` — 활성 멤버 목록\n"
            "\n"
            "`/help` — 이 도움말"
        )
        await self.send_message(text, chat_id)
