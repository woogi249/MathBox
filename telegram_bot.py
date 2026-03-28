"""Token Flex Dashboard — Telegram bot module.

경량 구현: python-telegram-bot 의존 없이 httpx로 Bot API를 직접 호출한다.
서버 프로세스 내부에서 polling loop로 동작하며,
알림 브로드캐스트와 명령 응답을 담당한다.

Commands:
  /rank       — 전체 랭킹 보드
  /me <id>    — 개인 상세 통계
  /today      — 오늘의 사용량 리더보드
  /stats      — 글로벌 집계 요약
  /milestone  — 최근 마일스톤 기록
  /daily <id> — 최근 7일 사용량 추이
  /help       — 명령어 목록
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

    def set_rank_callback(self, cb):
        self._rank_cb = cb

    def set_stats_callback(self, cb):
        self._stats_cb = cb

    def set_user_callback(self, cb):
        self._user_cb = cb

    def set_daily_callback(self, cb):
        self._daily_cb = cb

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

        parts = text.split(maxsplit=1)
        cmd = parts[0].lower().split("@")[0]  # /rank@botname → /rank
        arg = parts[1].strip() if len(parts) > 1 else ""

        dispatch = {
            "/rank": self._cmd_rank,
            "/me": self._cmd_me,
            "/today": self._cmd_today,
            "/stats": self._cmd_stats,
            "/milestone": self._cmd_milestone,
            "/daily": self._cmd_daily,
            "/help": self._cmd_help,
            "/start": self._cmd_help,
        }

        handler = dispatch.get(cmd)
        if handler:
            await handler(chat_id, arg)

    # ------------------------------------------------------------------
    # /rank — 전체 랭킹
    # ------------------------------------------------------------------

    async def _cmd_rank(self, chat_id: str, _arg: str) -> None:
        if not self._rank_cb:
            await self.send_message("데이터를 불러올 수 없습니다.", chat_id)
            return
        board = await self._rank_cb()
        await self.send_message(self.format_rank_board(board), chat_id)

    # ------------------------------------------------------------------
    # /me <user_id> — 개인 상세 통계
    # ------------------------------------------------------------------

    async def _cmd_me(self, chat_id: str, arg: str) -> None:
        if not arg:
            await self.send_message("사용법: `/me <user_id>`", chat_id)
            return
        if not self._user_cb:
            await self.send_message("데이터를 불러올 수 없습니다.", chat_id)
            return

        data = await self._user_cb(arg)
        if not data:
            await self.send_message(f"`{arg}` 유저를 찾을 수 없습니다.", chat_id)
            return

        ms_text = ""
        if data["milestones"]:
            ms_list = [f"`{m['milestone']:,}`" for m in data["milestones"]]
            ms_text = f"\n🏅 달성 마일스톤: {', '.join(ms_list)}"

        text = (
            f"👤 *{data['user_id']}*\n"
            f"\n"
            f"🏆 순위: *#{data['rank']}* / {data['total_users']}\n"
            f"📊 누적: `{data['used_tokens']:,}` tokens\n"
            f"📅 오늘: `{data['today_tokens']:,}` tokens\n"
            f"🕐 마지막 보고: _{data['last_reported']}_"
            f"{ms_text}"
        )
        await self.send_message(text, chat_id)

    # ------------------------------------------------------------------
    # /today — 오늘의 리더보드
    # ------------------------------------------------------------------

    async def _cmd_today(self, chat_id: str, _arg: str) -> None:
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
    # /stats — 글로벌 집계
    # ------------------------------------------------------------------

    async def _cmd_stats(self, chat_id: str, _arg: str) -> None:
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
            f"👥 참여자: *{s['total_users']}*명\n"
            f"🔥 전체 토큰: `{s['total_tokens']:,}`\n"
            f"{leader_line}"
            f"{ms_line}"
            f"⚠️ 일일 한도: `{s['daily_limit']:,}`"
        )
        await self.send_message(text, chat_id)

    # ------------------------------------------------------------------
    # /milestone — 마일스톤 기록
    # ------------------------------------------------------------------

    async def _cmd_milestone(self, chat_id: str, _arg: str) -> None:
        if not self._rank_cb:
            await self.send_message("데이터를 불러올 수 없습니다.", chat_id)
            return

        # stats callback 통해 latest만 가져오기
        if self._stats_cb:
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
        else:
            await self.send_message("데이터를 불러올 수 없습니다.", chat_id)

    # ------------------------------------------------------------------
    # /daily <user_id> — 최근 7일 추이
    # ------------------------------------------------------------------

    async def _cmd_daily(self, chat_id: str, arg: str) -> None:
        if not arg:
            await self.send_message("사용법: `/daily <user_id>`", chat_id)
            return
        if not self._daily_cb:
            await self.send_message("데이터를 불러올 수 없습니다.", chat_id)
            return

        history = await self._daily_cb(arg)
        if not history:
            await self.send_message(f"`{arg}` 의 일별 기록이 없습니다.", chat_id)
            return

        max_tok = max(d["tokens"] for d in history) if history else 1
        lines = [f"📊 *{arg}* — 최근 {len(history)}일 추이", ""]
        for d in history:
            bar = self._bar(d["tokens"], max_tok, 10)
            lines.append(f"`{d['date'][5:]}` {bar} `{self._fmt_tokens(d['tokens'])}`")

        await self.send_message("\n".join(lines), chat_id)

    # ------------------------------------------------------------------
    # /help
    # ------------------------------------------------------------------

    async def _cmd_help(self, chat_id: str, _arg: str) -> None:
        text = (
            "🤖 *Token Flex Bot*\n"
            "\n"
            "`/rank` — 전체 랭킹 보드\n"
            "`/me <id>` — 개인 상세 통계\n"
            "`/today` — 오늘의 리더보드\n"
            "`/stats` — 글로벌 집계 요약\n"
            "`/milestone` — 최근 마일스톤 기록\n"
            "`/daily <id>` — 최근 7일 사용량 추이\n"
            "`/help` — 이 도움말"
        )
        await self.send_message(text, chat_id)
