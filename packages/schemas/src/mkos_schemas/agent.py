"""Agent run telemetry records."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_serializer

from .base import SCHEMA_VERSION, RequestId, WireModel
from .errors import ErrorReport

AgentName = Literal[
    "content",
    "keyword",
    "attribute",
    "image",
    "correction",
    "embedding",
    "quality",
]
Trigger = Literal["generate", "correct", "embed", "vision"]


class TokenUsage(WireModel):
    input: Annotated[int, Field(ge=0)] = 0
    output: Annotated[int, Field(ge=0)] = 0
    cached_input: Annotated[int, Field(ge=0)] = 0


class AgentRunRecord(WireModel):
    schema_version: str = SCHEMA_VERSION
    id: UUID
    tenant_id: UUID
    product_id: UUID | None = None
    listing_id: UUID | None = None
    listing_version_id: UUID | None = None
    request_id: RequestId
    job_id: UUID | None = None
    agent_name: AgentName
    trigger: Trigger
    attempt: Annotated[int, Field(ge=1, le=3)] = 1
    model_used: str | None = None
    prompt_version: str | None = None
    tokens: TokenUsage = Field(default_factory=TokenUsage)
    cost_usd: Annotated[
        Decimal,
        Field(
            max_digits=12,
            decimal_places=6,
            ge=0,
            description=(
                "USD as a decimal string with at most 6 decimal places, e.g. '0.000413' "
                "(NUMERIC(12,6)). Producers must quantize with "
                "Decimal(...).quantize(Decimal('0.000001'), ROUND_HALF_UP) before sending; "
                "floats with more digits are rejected, never rounded."
            ),
        ),
    ] = Decimal("0")
    duration_ms: Annotated[int, Field(ge=0)] = 0
    status: Literal["running", "completed", "failed", "timeout"]
    error_report: ErrorReport | None = None
    input_digest: str | None = None
    started_at: AwareDatetime
    created_at: AwareDatetime

    @field_serializer("cost_usd", when_used="json")
    def _serialize_cost_usd(self, value: Decimal) -> str:
        # NUMERIC(12,6): always six digits after the decimal, never a shortened "1.5".
        return format(value.quantize(Decimal("0.000001")), "f")
