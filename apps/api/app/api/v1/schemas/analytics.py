"""Operational analytics (owner step S23; T14: operational metrics only in v1)."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from mkos_schemas.base import WireModel
from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

Rate = Annotated[float | None, Field(ge=0.0, le=1.0, allow_inf_nan=False)]


class AnalyticsSummary(WireModel):
    from_: date = Field(alias="from")
    to: date
    listings_generated: Annotated[int, Field(ge=0)]
    approval_rate: Rate = None
    validation_pass_rate: Rate = None
    publish_success_rate: Rate = None

    @model_validator(mode="after")
    def _range(self) -> AnalyticsSummary:
        if self.from_ > self.to:
            raise PydanticCustomError("mkos_invalid", "'from' must not be after 'to'.")
        return self
