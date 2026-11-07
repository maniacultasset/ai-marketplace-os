"""The platform admin's AI credit card (SI10 D1): the pause, the last OpenRouter key check and
today's platform spend against its cap. Money is a decimal string with 6 places, null when not
known (an unlimited key has no limit or remaining)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from mkos_schemas.base import WireModel
from pydantic import Field

UsdText = Annotated[
    str,
    Field(
        pattern=r"^\d{1,6}\.\d{6}$",
        description="USD as a decimal string with 6 places.",
        examples=["9.600000"],
    ),
]


class AiCreditOut(WireModel):
    state: Literal["ok", "low", "paused", "unknown"] = Field(
        description=(
            "`paused`: AI is paused for every brand; `low`: the last check (within 15 minutes) "
            "is below the floor, so generation is refused; `unknown`: never checked or the "
            "check is stale; `ok` otherwise."
        )
    )
    paused_reason: Literal["credit_exhausted", "key_invalid"] | None = None
    paused_at: datetime | None = None
    limit_usd: UsdText | None = Field(default=None, description="The key's credit limit.")
    usage_usd: UsdText | None = Field(default=None, description="What the key has used.")
    remaining_usd: UsdText | None = Field(default=None, description="What is left of the limit.")
    floor_usd: UsdText = Field(description="Below this remaining credit, generation is refused.")
    checked_at: datetime | None = None
    today_spend_usd: UsdText = Field(
        description="Today's (UTC) platform AI spend plus live reservations."
    )
    daily_cap_usd: UsdText = Field(description="The platform's daily AI cap.")
