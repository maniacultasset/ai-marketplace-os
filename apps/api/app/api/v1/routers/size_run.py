"""V1.1: make a style's size variants from its saved size chart, only when the seller asks.
`product:import` (owner, operator). Another tenant's product is 404. Routes import no
`sqlalchemy` or `app.db`; domain errors render through the shared handlers."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from fastapi import Path as PathParam

from app.api.v1.deps import IdempotencyKey, get_current_principal
from app.api.v1.schemas.size_run import SizeRunIn, SizeRunOut
from app.api.v1.surface import OPS, route
from app.core.rbac import Principal
from app.services import size_run

router = APIRouter()
CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]


@route(router, OPS["products_size_run_create"])
async def products_size_run_create(
    product_id: Annotated[UUID, PathParam()],
    body: SizeRunIn,
    idempotency_key: IdempotencyKey,
    principal: CurrentPrincipal,
    request: Request,
    response: Response,
) -> SizeRunOut:
    """Make one variant per size the seller ticked, each a row of the saved size chart (save the
    chart first with `PATCH /products/{product_id}`). A standalone product becomes a size style;
    a style keeps its sizes and gains the new ones. SKUs are `<style sku>-<SIZE>`. 201 with the
    variants made; 200 with none when every size exists. A variant is 422 `FACT_LEVEL_INVALID`;
    a style split by colour, or a standalone holding a barcode, stock reference or its own size,
    is 422 `VARIATION_INVALID`; a size not on the chart 422 at `sizes[i]`; a taken SKU 409
    `SKU_CONFLICT`; a listing the marketplace has seen 409 `INVALID_STATUS_TRANSITION`; a
    generating listing 409 `LISTING_BUSY`. An unpublished listing written for the old family
    waits for a regenerate (an approval is voided)."""
    status, out = await size_run.create_size_run(
        principal, product_id, body.sizes, idempotency_key, request.url.path
    )
    response.status_code = status
    return SizeRunOut.model_validate(out)
