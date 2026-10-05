"""Analytics stubs (owner step S23; operational metrics only in v1, T14)."""

from __future__ import annotations

from datetime import date
from typing import Annotated, NoReturn

from fastapi import APIRouter, Depends, Query
from mkos_schemas.codes import ErrorCode

from app.api.errors import ApiError, NotImplementedYet
from app.api.v1.surface import OPS, route, stub

router = APIRouter()


class DateRange:
    """`from` and `to` (inclusive ISO dates); `from` after `to` is 422."""

    def __init__(
        self,
        from_: Annotated[date, Query(alias="from")],
        to: Annotated[date, Query()],
    ) -> None:
        if from_ > to:
            raise ApiError(
                422,
                ErrorCode.VALIDATION_FAILED,
                details=[{"loc": ["query", "from"], "msg": "'from' must not be after 'to'."}],
            )
        self.from_ = from_
        self.to = to


@route(router, OPS["analytics_get"])
@stub
async def analytics_get(period: Annotated[DateRange, Depends()]) -> NoReturn:
    """Operational metrics for the range (an alias of `/analytics/summary`)."""
    raise NotImplementedYet("analytics_get")


@route(router, OPS["analytics_summary"])
@stub
async def analytics_summary(period: Annotated[DateRange, Depends()]) -> NoReturn:
    """Volume, approval rate, validation pass rate and publish success for the range."""
    raise NotImplementedYet("analytics_summary")
