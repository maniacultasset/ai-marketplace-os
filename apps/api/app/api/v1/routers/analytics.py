"""Analytics (S23; operational metrics only in v1, TEAM-QUESTIONS T14).

`GET /analytics` and `GET /analytics/summary` answer the same `AnalyticsSummary` for the caller's
tenant (from the token, never a parameter): the four metrics, their counts and the marketplace
breakdown for the inclusive UTC-day range `from`..`to` (both optional; see
`app.services.analytics.resolve_range`). A bad range is 422 at the parameter at fault; a third
concurrent request of the tenant is 429; a read that runs out of its 5 s budget is 500
`INTERNAL_ERROR` with no driver text."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from mkos_schemas.codes import ErrorCode

from app.api.errors import ApiError
from app.api.v1.deps import get_current_principal
from app.api.v1.schemas.analytics import AnalyticsSummary
from app.api.v1.surface import OPS, route
from app.core.rbac import Principal
from app.services import analytics

router = APIRouter()
CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]
From = Annotated[date | None, Query(alias="from")]
To = Annotated[date | None, Query()]
BUSY_RETRY_AFTER_SECONDS = "5"


async def _summary(principal: Principal, from_: date | None, to: date | None) -> AnalyticsSummary:
    try:
        body = await analytics.get_summary(principal.tenant_id, principal.user_id, from_, to)
    except analytics.AnalyticsRangeError as exc:
        raise ApiError(
            422,
            ErrorCode.VALIDATION_FAILED,
            details=[{"loc": ["query", exc.field], "msg": exc.reason}],
        ) from None
    except analytics.AnalyticsBusyError:
        raise ApiError(
            429, ErrorCode.RATE_LIMITED, headers={"Retry-After": BUSY_RETRY_AFTER_SECONDS}
        ) from None
    except analytics.AnalyticsBudgetError:
        raise ApiError(500, ErrorCode.INTERNAL_ERROR) from None
    return AnalyticsSummary.model_validate(body)


@route(router, OPS["analytics_get"])
async def analytics_get(
    principal: CurrentPrincipal, from_: From = None, to: To = None
) -> AnalyticsSummary:
    """Operational metrics for the range (an alias of `/analytics/summary`)."""
    return await _summary(principal, from_, to)


@route(router, OPS["analytics_summary"])
async def analytics_summary(
    principal: CurrentPrincipal, from_: From = None, to: To = None
) -> AnalyticsSummary:
    """Volume, approval rate, validation pass rate and publish success for the range."""
    return await _summary(principal, from_, to)
