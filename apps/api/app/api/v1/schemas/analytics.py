"""Operational analytics (owner step S23; T14: operational metrics only in v1).

`AnalyticsSummary` keeps S3's four headline fields and gains (S23, additive MINOR) the raw
`counts` behind each rate and a `by_marketplace` breakdown. A rate is a share in [0, 1] rounded to
4 places, or null when its denominator is 0 (nothing to rate yet)."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from mkos_schemas.base import WireModel
from mkos_schemas.enums import Marketplace
from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

Rate = Annotated[float | None, Field(ge=0.0, le=1.0, allow_inf_nan=False)]
Count = Annotated[int, Field(ge=0)]


class AnalyticsCounts(WireModel):
    """The counts each rate is computed from, for the range."""

    listings_generated: Count
    approvals: Count
    rejections: Count
    validation_first_pass_passed: Count
    validation_first_pass_total: Count
    publish_succeeded: Count
    publish_failed: Count


class MarketplaceBreakdown(WireModel):
    """The same metrics for one marketplace."""

    marketplace: Marketplace
    listings_generated: Count
    approval_rate: Rate = None
    validation_pass_rate: Rate = None
    publish_success_rate: Rate = None
    counts: AnalyticsCounts


class AnalyticsSummary(WireModel):
    from_: date = Field(alias="from")
    to: date
    listings_generated: Annotated[int, Field(ge=0)]
    approval_rate: Rate = None
    validation_pass_rate: Rate = None
    publish_success_rate: Rate = None
    counts: AnalyticsCounts | None = None
    by_marketplace: Annotated[list[MarketplaceBreakdown] | None, Field(max_length=2)] = None

    @model_validator(mode="after")
    def _range(self) -> AnalyticsSummary:
        if self.from_ > self.to:
            raise PydanticCustomError("mkos_invalid", "'from' must not be after 'to'.")
        return self
