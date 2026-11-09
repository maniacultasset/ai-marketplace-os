"""Operational analytics (owner step S23; T14: operational metrics only in v1).

`AnalyticsSummary` keeps S3's four headline fields and gains (S23, additive MINOR) the raw
`counts` behind each rate and a `by_marketplace` breakdown. A rate is a share in [0, 1] rounded to
4 places, or null when its denominator is 0 (nothing to rate yet)."""

from __future__ import annotations

from datetime import date
from typing import Annotated
from uuid import UUID

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


# ---- SI14: the quality dashboard (BE-13) ----


class QualityMarket(WireModel):
    marketplace: Marketplace
    listings_generated: Count
    first_pass_rate: Rate = None
    blocked_rate: Rate = None
    edit_rate: Rate = None
    cost_usd: Annotated[float, Field(ge=0.0, allow_inf_nan=False)]
    cost_per_listing_usd: Annotated[float | None, Field(ge=0.0, allow_inf_nan=False)] = None


class EditedField(WireModel):
    path: Annotated[str, Field(max_length=192)]
    count: Count


class QualityTenant(WireModel):
    """Platform view only: ids and counts, never a name or any text."""

    tenant_id: UUID
    listings_generated: Count
    first_pass_rate: Rate = None
    blocked_rate: Rate = None
    edit_rate: Rate = None
    cost_usd: Annotated[float, Field(ge=0.0, allow_inf_nan=False)]


class QualitySummary(WireModel):
    """First-pass rate (approved with no correction and no edit), blocked rate (reached `blocked`
    in the range, by the worker or a user), edit rate (approved after a person edited it) and the
    model cost per generated listing. A rate is null when nothing was generated or approved yet.
    The cost counts the generation runs of the listings; classify and column-map runs have no
    listing and are not included."""

    from_: date = Field(alias="from")
    to: date
    listings_generated: Count
    first_pass_rate: Rate = None
    blocked_rate: Rate = None
    edit_rate: Rate = None
    cost_per_listing_usd: Annotated[float | None, Field(ge=0.0, allow_inf_nan=False)] = None
    by_marketplace: Annotated[list[QualityMarket], Field(max_length=2)] = Field(
        default_factory=list
    )
    edited_fields: Annotated[list[EditedField], Field(max_length=20)] = Field(default_factory=list)
    by_tenant: Annotated[list[QualityTenant] | None, Field(max_length=200)] = None

    @model_validator(mode="after")
    def _range(self) -> QualitySummary:
        if self.from_ > self.to:
            raise PydanticCustomError("mkos_invalid", "'from' must not be after 'to'.")
        return self
