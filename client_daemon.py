"""Token Flex Dashboard — Client daemon (local-only, API key never leaves host)."""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime

import httpx

# ---------------------------------------------------------------------------
# Configuration (env vars)
# ---------------------------------------------------------------------------

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
USER_ID = os.environ.get("TOKENFLEX_USER_ID", "")
SERVER_URL = os.environ.get("TOKENFLEX_SERVER", "http://localhost:8000")
REPORT_INTERVAL = int(os.environ.get("TOKENFLEX_INTERVAL", "600"))  # 10분

ANTHROPIC_USAGE_URL = "https://api.anthropic.com/v1/organizations/usage"

# ---------------------------------------------------------------------------
# Anthropic Usage API 조회
# ---------------------------------------------------------------------------


async def fetch_used_tokens(client: httpx.AsyncClient) -> int | None:
    """Anthropic Usage API에서 현재 토큰 사용량을 조회한다.

    실패 시 None을 반환 — Circuit Breaker 패턴에 따라 재시도 없이 넘어간다.
    """
    try:
        resp = await client.get(
            ANTHROPIC_USAGE_URL,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()
        # Usage API 응답에서 누적 토큰 추출 (API 스펙에 따라 조정 필요)
        return int(data.get("total_tokens", 0))
    except Exception as exc:
        print(f"[{datetime.utcnow().isoformat()}] Usage fetch failed: {exc}")
        return None


# ---------------------------------------------------------------------------
# 서버 보고
# ---------------------------------------------------------------------------


async def report_to_server(client: httpx.AsyncClient, used_tokens: int) -> None:
    """중앙 서버에 사용량을 POST한다. 실패 시 로그만 남기고 넘어간다."""
    payload = {
        "user_id": USER_ID,
        "used_tokens": used_tokens,
        "reported_at": datetime.utcnow().isoformat(),
    }
    try:
        resp = await client.post(
            f"{SERVER_URL}/report", json=payload, timeout=10.0
        )
        resp.raise_for_status()
        body = resp.json()
        print(
            f"[{datetime.utcnow().isoformat()}] Reported {used_tokens:,} tokens "
            f"→ rank #{body.get('rank')}/{body.get('total_users')}"
        )
    except Exception as exc:
        print(f"[{datetime.utcnow().isoformat()}] Report failed: {exc}")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


async def main() -> None:
    if not ANTHROPIC_API_KEY:
        sys.exit("ERROR: ANTHROPIC_API_KEY 환경변수가 설정되지 않았습니다.")
    if not USER_ID:
        sys.exit("ERROR: TOKENFLEX_USER_ID 환경변수가 설정되지 않았습니다.")

    print(
        f"Token Flex client started — user={USER_ID}, "
        f"server={SERVER_URL}, interval={REPORT_INTERVAL}s"
    )

    async with httpx.AsyncClient() as client:
        while True:
            tokens = await fetch_used_tokens(client)
            if tokens is not None:
                await report_to_server(client, tokens)
            # Circuit Breaker: 성공이든 실패든 다음 주기까지 대기
            await asyncio.sleep(REPORT_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
