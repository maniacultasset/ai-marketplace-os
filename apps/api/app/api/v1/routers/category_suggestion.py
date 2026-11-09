"""The category suggestion (owner step SI14): classify a product into a suggested category the
seller confirms with PATCH. `product:import` (owner, operator). Another tenant's product is 404.
Routes import no `sqlalchemy` or `app.db`; domain errors render through the shared handlers."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from fastapi import Path as PathParam
from mkos_schemas.codes import ErrorCode

from app.api.errors import ApiError
from app.api.v1.deps import IdempotencyKey, get_current_principal
from app.api.v1.schemas.category_suggestion import CategorySuggestionAccepted
from app.api.v1.surface import OPS, route
from app.core.rbac import Principal
from app.services import category_suggest
from app.services.budget import BudgetExceededError, CreditPausedError

router = APIRouter()
CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]


@route(router, OPS["products_category_suggest"])
async def products_category_suggest(
    product_id: Annotated[UUID, PathParam()],
    idempotency_key: IdempotencyKey,
    principal: CurrentPrincipal,
    request: Request,
    response: Response,
) -> CategorySuggestionAccepted:
    """Suggest a category for a style or standalone product from its text and photos. 202 names
    the job (poll the product for `suggestions.category`); 200 `suggested` when the stored
    suggestion already matches the product's current text, photos and categories (no new job).
    The category is never set by this call: confirm a candidate with `PATCH
    /products/{product_id}`. A variant is 422 `FACT_LEVEL_INVALID`. While AI is paused for every
    brand it is 503 with the pause's code (SI10); past the daily cap 429. While the classifier is
    switched off (`category_suggest_enabled: false` on the product) it is 200 `unavailable` with no
    job and nothing spent."""
    try:
        status, body = await category_suggest.request_suggestion(
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
    return CategorySuggestionAccepted.model_validate(body)
