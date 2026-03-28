"""Token Flex Dashboard — Telegram bot module.

경량 구현: python-telegram-bot 의존 없이 httpx로 Bot API를 직접 호출한다.
서버 프로세스 내부에서 polling loop로 동작하며,
알림 브로드캐스트와 /rank 명령 응답을 담당한다.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from schemas import AlertEvent, RankBoard

logger = logging.getLogger("tokenflex.telegram")

# ---------------------------------------------------------------------------
# Telegram Bot API wrapper (httpx 기반, 외부 라이브러리 불필요)
# ---------------------------------------------------------------------------


class TelegramBot:
    """최소 Telegram Bot API 클라이언트."""

    BASE = "https://api.telegram.org/bot{token}"

    def __init__(self, token: str, chat_id: str) -> None:
        self.token = token
        self.chat_id = chat_id  # 단톡방 chat_id
        self._base = self.BASE.format(token=token)
        self._client: httpx.AsyncClient | None = None
        self._offset: int = 0
        self._running = False
        # /rank 콜백 — 서버에서 주입
        self._rank_callback: callable | None = None

    async def start(self) -> None:
        self._client = httpx.AsyncClient(timeout=30.0)
        self._running = True
        logger.info("Telegram bot polling started (chat_id=%s)", self.chat_id)

    async def stop(self) -> None:
        self._running = False
        if self._client:
            await self._client.aclose()
            self._client = None

    def set_rank_callback(self, cb: callable) -> None:
        self._rank_callback = cb

    # ------------------------------------------------------------------
    # 메시지 발송
    # ------------------------------------------------------------------

    async def send_message(self, text: str, chat_id: str | None = None) -> None:
        """텔레그램 메시지 전송. 실패 시 로그만 남긴다."""
        if not self._client:
            return
        target = chat_id or self.chat_id
        try:
            resp = await self._client.post(
                f"{self._base}/sendMessage",
                json={
                    "chat_id": target,
                    "text": text,
                    "parse_mode": "Markdown",
                },
            )
            if resp.status_code != 200:
                logger.warning("sendMessage failed: %s", resp.text)
        except Exception as exc:
            logger.warning("sendMessage error: %s", exc)

    # ------------------------------------------------------------------
    # 알림 브로드캐스트
    # ------------------------------------------------------------------

    async def broadcast_alert(self, event: AlertEvent) -> None:
        await self.send_message(event.message)

    # ------------------------------------------------------------------
    # 랭킹 포맷터
    # ------------------------------------------------------------------

    @staticmethod
    def format_rank_board(board: RankBoard) -> str:
        if not board.board:
            return "📊 *Token Flex Ranking*\n\n등록된 유저가 없습니다."

        lines = ["🏆 *Token Flex Ranking*", ""]
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        for entry in board.board:
            medal = medals.get(entry.rank, f"{entry.rank}.")
            tokens_fmt = f"{entry.used_tokens:,}"
            lines.append(f"{medal} *{entry.user_id}* — `{tokens_fmt}` tokens")

        lines.append("")
        lines.append(
            f"_Updated: {board.generated_at.strftime('%Y-%m-%d %H:%M UTC')}_"
        )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Polling loop (long-polling, 서버 lifespan에서 task로 실행)
    # ------------------------------------------------------------------

    async def poll_loop(self) -> None:
        """getUpdates long-polling으로 /rank 등 명령을 수신한다."""
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
        text = msg.get("text", "")
        chat_id = str(msg.get("chat", {}).get("id", ""))

        if text.strip().startswith("/rank"):
            await self._cmd_rank(chat_id)
        elif text.strip().startswith("/help"):
            await self._cmd_help(chat_id)

    async def _cmd_rank(self, chat_id: str) -> None:
        if not self._rank_callback:
            await self.send_message("랭킹 데이터를 불러올 수 없습니다.", chat_id)
            return
        board = await self._rank_callback()
        await self.send_message(self.format_rank_board(board), chat_id)

    async def _cmd_help(self, chat_id: str) -> None:
        await self.send_message(
            "*Token Flex Bot Commands*\n"
            "/rank — 현재 토큰 사용량 랭킹 조회\n"
            "/help — 도움말",
            chat_id,
        )
