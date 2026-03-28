"""
Pydantic 스키마 & 검증 하네스
- API Key 정규표현식 검증
- Anthropic Usage API 응답 무결성 검증
- 대시보드 렌더링용 DTO
"""

import re
from datetime import date
from typing import Annotated

from pydantic import BaseModel, Field, field_validator, model_validator


# ── 1. API Key 검증 ───────────────────────────────────────────────────────────

_ANTHROPIC_KEY_RE = re.compile(r"^sk-ant-api\d{2}-[A-Za-z0-9\-_]{93}AA$")


class ApiKeyInput(BaseModel):
    """DM으로 수신된 API 키 원문을 검증하는 진입점 스키마."""

    raw: str = Field(..., min_length=10, max_length=200, strip_whitespace=True)

    @field_validator("raw")
    @classmethod
    def validate_format(cls, v: str) -> str:
        if not _ANTHROPIC_KEY_RE.match(v):
            raise ValueError(
                "유효하지 않은 Anthropic API Key 형식입니다.\n"
                "형식: sk-ant-api__-<93자>AA"
            )
        return v


# ── 2. Anthropic Usage API 응답 스키마 ────────────────────────────────────────

class UsagePeriod(BaseModel):
    start_time: str
    end_time: str


class UsageBucket(BaseModel):
    """GET /v1/usage 응답의 단일 버킷."""

    start_time: str
    end_time: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_read_input_tokens: int = Field(default=0, ge=0)
    cache_creation_input_tokens: int = Field(default=0, ge=0)

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def efficiency_score(self) -> float:
        """캐시 히트율 (0~1). 캐시 토큰이 없으면 0."""
        denom = self.input_tokens + self.cache_read_input_tokens
        if denom == 0:
            return 0.0
        return self.cache_read_input_tokens / denom


class UsageResponse(BaseModel):
    """Anthropic Usage API 전체 응답."""

    data: list[UsageBucket] = Field(default_factory=list)
    has_more: bool = False

    @model_validator(mode="after")
    def check_non_negative_totals(self) -> "UsageResponse":
        for bucket in self.data:
            if bucket.input_tokens < 0 or bucket.output_tokens < 0:
                raise ValueError("토큰 수는 음수일 수 없습니다.")
        return self


# ── 3. 대시보드 DTO ───────────────────────────────────────────────────────────

class MemberStat(BaseModel):
    rank: int
    display_name: str
    total_input: int = 0
    total_output: int = 0
    total_cache_read: int = 0
    total_cache_write: int = 0
    error: str | None = None  # Circuit Breaker: 실패 시 사유 기록

    @property
    def total_tokens(self) -> int:
        return self.total_input + self.total_output

    @property
    def efficiency_pct(self) -> float:
        denom = self.total_input + self.total_cache_read
        if denom == 0:
            return 0.0
        return round(self.total_cache_read / denom * 100, 1)

    def render_row(self) -> str:
        if self.error:
            return f"{self.rank}. {self.display_name}  ⚠️ {self.error}"
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(self.rank, f"{self.rank}.")
        return (
            f"{medal} {self.display_name}\n"
            f"   ├ 입력: {self.total_input:,}  출력: {self.total_output:,}\n"
            f"   ├ 캐시히트: {self.total_cache_read:,} ({self.efficiency_pct}%)\n"
            f"   └ 합계: {self.total_tokens:,} tokens"
        )


class DashboardPayload(BaseModel):
    chat_title: str
    report_date: date
    members: list[MemberStat]

    def render(self) -> str:
        header = (
            f"📊 *{self.chat_title}* 일일 토큰 리더보드\n"
            f"📅 {self.report_date.isoformat()}\n"
            f"{'─' * 30}\n"
        )
        if not self.members:
            return header + "_등록된 멤버가 없습니다._"
        body = "\n\n".join(m.render_row() for m in self.members)
        footer = f"\n{'─' * 30}\n총 {len(self.members)}명 참여 중"
        return header + body + footer
