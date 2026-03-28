"""
MathBox – Private Token Flex Dashboard Bot
메인 진입점. aiogram Router로 DM/Group 명령어를 완전 분리.
"""

import asyncio
import logging
import os
from datetime import date, datetime, timezone

import httpx
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from pydantic import ValidationError

import db
from schemas import (
    ApiKeyInput,
    DashboardPayload,
    MemberStat,
    UsageResponse,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

BOT_TOKEN: str = os.environ["BOT_TOKEN"]

# ── Routers ────────────────────────────────────────────────────────────────────

dm_router = Router(name="dm")          # 1:1 DM 전용
group_router = Router(name="group")    # 그룹방 전용

# DM 라우터: ChatType.PRIVATE 메시지만 처리
dm_router.message.filter(F.chat.type == ChatType.PRIVATE)

# 그룹 라우터: GROUP / SUPERGROUP 메시지만 처리
group_router.message.filter(
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP})
)


# ── DM 핸들러 ──────────────────────────────────────────────────────────────────

@dm_router.message(CommandStart())
async def dm_start(msg: Message) -> None:
    await msg.answer(
        "안녕하세요! 그룹방 토큰 랭킹 봇입니다.\n\n"
        "Anthropic Read-only API Key를 등록하려면:\n"
        "`/register sk-ant-api__-...`\n\n"
        "⚠️ 절대로 *그룹방*에 키를 보내지 마세요.",
        parse_mode=ParseMode.MARKDOWN,
    )


@dm_router.message(Command("register"))
async def dm_register(msg: Message) -> None:
    """API 키 등록 – DM에서만 허용."""
    args = (msg.text or "").split(maxsplit=1)
    if len(args) < 2:
        await msg.answer("사용법: `/register <your-api-key>`", parse_mode=ParseMode.MARKDOWN)
        return

    raw_key = args[1].strip()
    try:
        validated = ApiKeyInput(raw=raw_key)
    except ValidationError as exc:
        err = exc.errors()[0]["msg"]
        await msg.answer(f"❌ 키 검증 실패:\n{err}")
        return

    user = msg.from_user
    db.upsert_user(
        user_id=user.id,
        username=user.username or user.full_name,
        api_key=validated.raw,
    )
    await msg.answer("✅ API 키가 안전하게 저장되었습니다.")


@dm_router.message(Command("mystats"))
async def dm_mystats(msg: Message) -> None:
    """개인 오늘 사용량 조회 (DM 전용)."""
    row = db.get_user(msg.from_user.id)
    if not row:
        await msg.answer("등록된 API 키가 없습니다. `/register` 로 먼저 등록하세요.")
        return

    today_str = date.today().isoformat()
    result = await _fetch_usage(row["api_key"], today_str)
    if isinstance(result, str):          # 에러 메시지
        await msg.answer(f"⚠️ 조회 실패: {result}")
        return

    total_in = sum(b.input_tokens for b in result.data)
    total_out = sum(b.output_tokens for b in result.data)
    await msg.answer(
        f"📈 오늘({today_str}) 내 사용량\n"
        f"입력: {total_in:,}  출력: {total_out:,}\n"
        f"합계: {total_in + total_out:,} tokens"
    )


# 그룹방 API 키 직접 입력 차단
@dm_router.message(F.text.regexp(r"sk-ant-api"))
async def dm_key_leak_guard(msg: Message) -> None:
    """DM에서도 텍스트에 키가 포함된 경우 /register 유도."""
    await msg.answer(
        "키를 텍스트로 직접 보내지 마세요.\n`/register <key>` 명령어를 사용하세요.",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── Group 핸들러 ───────────────────────────────────────────────────────────────

@group_router.message(F.text.regexp(r"sk-ant-api"))
async def group_key_leak_block(msg: Message) -> None:
    """그룹방에서 API 키 패턴이 감지되면 즉시 경고."""
    await msg.reply(
        "🚨 API 키가 감지되었습니다! 즉시 삭제하고 키를 재발급하세요.\n"
        "키 등록은 반드시 봇과 *1:1 DM*으로 진행하세요.",
        parse_mode=ParseMode.MARKDOWN,
    )


@group_router.my_chat_member()
async def on_bot_join(event, bot: Bot) -> None:
    """봇이 그룹에 초대되면 그룹 세션 등록."""
    chat = event.chat
    if event.new_chat_member.status in ("member", "administrator"):
        db.upsert_group(chat.id, chat.title)
        log.info("그룹 등록: %s (%d)", chat.title, chat.id)


@group_router.message(Command(commands=["rank", "dashboard"]))
async def group_dashboard(msg: Message) -> None:
    """/rank 또는 /dashboard – 그룹 리더보드 출력."""
    chat = msg.chat
    db.upsert_group(chat.id, chat.title)

    # 메시지를 보낸 사람도 멤버로 등록
    if msg.from_user:
        db.add_member(chat.id, msg.from_user.id)

    members = db.get_group_registered_members(chat.id)
    if not members:
        await msg.reply(
            "아직 이 그룹에 등록된 API 키가 없습니다.\n"
            "봇과 1:1 DM으로 `/register <key>` 를 보내세요.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    today_str = date.today().isoformat()
    stats: list[MemberStat] = []

    # Circuit Breaker: 멤버별로 독립 처리 – 하나 실패해도 전체 중단 없음
    for idx, member in enumerate(members, start=1):
        display = f"@{member['username']}" if member["username"] else f"User{member['user_id']}"
        result = await _fetch_usage(member["api_key"], today_str)

        if isinstance(result, str):      # 에러 메시지 수신
            stats.append(MemberStat(rank=idx, display_name=display, error=result))
            continue

        # DB에 기록 (중복 방지를 위해 오늘 날짜로 이미 기록된 값은 sum에 포함됨)
        total_in = sum(b.input_tokens for b in result.data)
        total_out = sum(b.output_tokens for b in result.data)
        total_cr = sum(b.cache_read_input_tokens for b in result.data)
        total_cw = sum(b.cache_creation_input_tokens for b in result.data)

        stats.append(
            MemberStat(
                rank=idx,
                display_name=display,
                total_input=total_in,
                total_output=total_out,
                total_cache_read=total_cr,
                total_cache_write=total_cw,
            )
        )

    # 토큰 합계 내림차순 재정렬 (에러 멤버는 후순위)
    stats.sort(key=lambda s: (s.error is None, s.total_tokens), reverse=True)
    for i, s in enumerate(stats, start=1):
        s.rank = i

    payload = DashboardPayload(
        chat_title=chat.title or str(chat.id),
        report_date=date.today(),
        members=stats,
    )
    await msg.reply(payload.render(), parse_mode=ParseMode.MARKDOWN)


@group_router.message(Command("join"))
async def group_join(msg: Message) -> None:
    """그룹 멤버 수동 등록 – 이미 DM으로 키를 등록한 사람이 그룹에 참여 선언."""
    if not msg.from_user:
        return
    user_row = db.get_user(msg.from_user.id)
    if not user_row:
        await msg.reply(
            "먼저 봇 DM으로 `/register <key>` 를 입력해 키를 등록하세요.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    db.upsert_group(msg.chat.id, msg.chat.title)
    db.add_member(msg.chat.id, msg.from_user.id)
    await msg.reply(f"✅ {msg.from_user.full_name} 님이 랭킹에 참가했습니다!")


# ── Anthropic Usage API 호출 (Circuit Breaker) ──────────────────────────────

async def _fetch_usage(api_key: str, date_str: str) -> UsageResponse | str:
    """
    Anthropic Usage API 호출.
    실패 시 에러 문자열 반환 (예외 전파 없음 – Circuit Breaker 패턴).
    """
    url = "https://api.anthropic.com/v1/usage"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "usage-reporting-2025-01-01",
    }
    params = {
        "start_time": f"{date_str}T00:00:00Z",
        "end_time": f"{date_str}T23:59:59Z",
        "bucket_duration": "day",
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers, params=params)

        if resp.status_code == 429:
            return "Rate Limit"
        if resp.status_code == 401:
            return "키 만료/무효"
        if resp.status_code != 200:
            return f"API 오류 {resp.status_code}"

        return UsageResponse.model_validate(resp.json())

    except httpx.TimeoutException:
        return "Timeout"
    except Exception as exc:
        log.warning("Usage fetch error: %s", exc)
        return "조회 실패"


# ── 앱 진입점 ──────────────────────────────────────────────────────────────────

async def main() -> None:
    db.init_db()

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()

    # 라우터 등록 순서: DM 먼저, 그룹 다음
    dp.include_router(dm_router)
    dp.include_router(group_router)

    log.info("Bot starting...")
    await dp.start_polling(bot, allowed_updates=["message", "my_chat_member"])


if __name__ == "__main__":
    asyncio.run(main())
