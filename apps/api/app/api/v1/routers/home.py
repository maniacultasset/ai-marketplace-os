"""The Home summary (client feedback 11 Nov 2026, item 6). Any member of the tenant; the counts
of `needs_you` follow the caller's role (`app.services.home`)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from mkos_schemas.codes import ErrorCode

from app.api.errors import ApiError
from app.api.v1.deps import get_current_principal
from app.api.v1.schemas.home import HomeBrandsOut, HomeSummaryOut
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


@route(router, OPS["home_brands"])
async def home_brands(principal: CurrentPrincipal) -> HomeBrandsOut:
    """Client feedback item 18: each of the caller's own brands (active memberships in live
    brands, by name) with its exact listing counts, products and what waits on the caller there
    for the caller's role in it. Each brand is counted in its own transaction under its own
    tenant, so RLS scopes every count; no statement reads two brands. A read past its 5 s budget
    answers 500 `INTERNAL_ERROR`, as the summary does."""
    try:
        body = await home.get_brands(principal)
    except AnalyticsBudgetError:
        raise ApiError(500, ErrorCode.INTERNAL_ERROR) from None
    return HomeBrandsOut.model_validate(body)
