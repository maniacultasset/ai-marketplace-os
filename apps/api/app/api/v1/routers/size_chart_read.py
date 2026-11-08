"""The size-chart read (owner step SI12): read the product's size-chart photo into a suggestion
the seller edits and saves. `product:import` (owner, operator). Another tenant's product is 404.
Routes import no `sqlalchemy` or `app.db`; domain errors render through the shared handlers."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from fastapi import Path as PathParam
from mkos_schemas.codes import ErrorCode

from app.api.errors import ApiError
from app.api.v1.deps import IdempotencyKey, get_current_principal
from app.api.v1.schemas.size_chart_read import SizeChartReadOut
from app.api.v1.surface import OPS, route
from app.core.rbac import Principal
from app.services import size_chart_read
from app.services.budget import BudgetExceededError, CreditPausedError

router = APIRouter()
CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]


@route(router, OPS["size_chart_read_create"])
async def size_chart_read_create(
    product_id: Annotated[UUID, PathParam()],
    idempotency_key: IdempotencyKey,
    principal: CurrentPrincipal,
    request: Request,
    response: Response,
) -> SizeChartReadOut:
    """Read the product's size-chart photo (a style or standalone product) into a suggestion. 202
    names the job (poll the product for `size_chart_suggestion`); 200 answers a photo already
    read (`read` or `unreadable`) with no new job. Nothing is saved as a fact: save the edited
    chart with `PATCH /products/{product_id}` and `size_chart_image_id`. A variant is 422
    `FACT_LEVEL_INVALID`, no size-chart photo 422, a photo still being checked 409. While AI is
    paused for every brand it is 503 with the pause's code (SI10); past the daily cap 429."""
    try:
        status, body = await size_chart_read.request_read(
            principal, product_id, idempotency_key, request.url.path
        )
    except BudgetExceededError as exc:
        raise ApiError(
            429,
            ErrorCode.BUDGET_EXCEEDED,
            exc.message,
            headers={"Retry-After": str(exc.retry_after_s)},
        ) from None
    except CreditPausedError as exc:
        raise ApiError(503, exc.code, exc.message) from None
    response.status_code = status
    return SizeChartReadOut.model_validate(body)
