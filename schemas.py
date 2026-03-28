"""Token Flex Dashboard — Shared Pydantic schemas (communication protocol)."""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, constr


# ---------------------------------------------------------------------------
# Client → Server
# ---------------------------------------------------------------------------


class UsagePayload(BaseModel):
    """Client → Server: 10분 주기 토큰 사용량 보고 페이로드."""

    user_id: constr(min_length=1, max_length=64) = Field(
        ..., description="고유 유저 식별자 (e.g. GitHub handle)"
    )
    used_tokens: int = Field(
        ..., ge=0, description="보고 시점까지의 누적 토큰 사용량"
    )
    reported_at: datetime = Field(
        default_factory=datetime.utcnow,
        description="클라이언트 측 보고 시각 (UTC)",
    )


# ---------------------------------------------------------------------------
# Server → Client
# ---------------------------------------------------------------------------


class UsageResponse(BaseModel):
    """Server → Client: 수신 확인 응답."""

    status: str = "ok"
    rank: int | None = Field(None, description="현재 유저 랭킹 (1-based)")
    total_users: int = 0


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


class RankEntry(BaseModel):
    """랭킹 보드 한 줄."""

    rank: int
    user_id: str
    used_tokens: int
    last_reported_at: datetime


class RankBoard(BaseModel):
    """Server → Telegram / API: 전체 랭킹 응답."""

    board: list[RankEntry]
    generated_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Milestone / Alert
# ---------------------------------------------------------------------------

# 토큰 마일스톤 기준점 (누적 토큰)
MILESTONES = [
    100_000,
    500_000,
    1_000_000,
    5_000_000,
    10_000_000,
    50_000_000,
    100_000_000,
]

DAILY_LIMIT = 10_000_000  # 일일 최대 한도 기본값


class AlertType(str, Enum):
    MILESTONE = "milestone"
    DAILY_LIMIT = "daily_limit"
    RANK_CHANGE = "rank_change"


class AlertEvent(BaseModel):
    """서버 내부에서 발생하는 알림 이벤트."""

    alert_type: AlertType
    user_id: str
    message: str
    used_tokens: int
    triggered_at: datetime = Field(default_factory=datetime.utcnow)
