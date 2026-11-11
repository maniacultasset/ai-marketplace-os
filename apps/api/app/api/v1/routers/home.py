"""The Home summary (client feedback 11 Nov 2026, item 6). Any member of the tenant; the counts
of `needs_you` follow the caller's role (`app.services.home`)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.v1.deps import get_current_principal
from app.api.v1.schemas.home import HomeSummaryOut
from app.api.v1.surface import OPS, route
from app.core.rbac import Principal
from app.services import home

router = APIRouter()
CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]


@route(router, OPS["home_summary"])
async def home_summary(principal: CurrentPrincipal) -> HomeSummaryOut:
    """Exact counts of the tenant's listings in one bucket each, the caller's work (blocked
    listings, blocking questions, listings to approve and to publish), the top 5 things to do,
    the 8 newest agent actions, and each marketplace's account and next step."""
    return HomeSummaryOut.model_validate(await home.get_summary(principal))
