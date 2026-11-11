"""The Home summary (client feedback 11 Nov 2026, item 6). Any member of the tenant; the counts
of `needs_you` follow the caller's role (`app.services.home`)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from mkos_schemas.codes import ErrorCode

from app.api.errors import ApiError
from app.api.v1.deps import get_current_principal
from app.api.v1.schemas.home import HomeSummaryOut
from app.api.v1.surface import OPS, route
from app.core.rbac import Principal
from app.repositories.analytics import AnalyticsBudgetError
from app.services import home

router = APIRouter()
CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]


@route(router, OPS["home_summary"])
async def home_summary(principal: CurrentPrincipal) -> HomeSummaryOut:
    """Exact counts of the tenant's listings in one bucket each, the caller's work (blocked
    listings, blocking questions, listings to approve and to publish), the top 5 things to do,
    the 8 newest agent actions, and each marketplace's account and next step. A read past its 5 s
    budget answers 500 `INTERNAL_ERROR` with no driver text, as the analytics reads do."""
    try:
        body = await home.get_summary(principal)
    except AnalyticsBudgetError:
        raise ApiError(500, ErrorCode.INTERNAL_ERROR) from None
    return HomeSummaryOut.model_validate(body)
