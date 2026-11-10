"""Product delete (owner step PD1): `DELETE /products/{product_id}` and `POST
/products/bulk-delete`. `settings:manage` (owner, admin). Another tenant's product, an unknown id
and an already deleted one are the same 404. Routes import no `sqlalchemy` or `app.db`; domain
errors render through the shared handlers; the service is `app.services.product_delete`."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from fastapi import Path as PathParam

from app.api.v1.deps import IdempotencyKey, get_current_principal
from app.api.v1.schemas.product_delete import (
    ProductBulkDeleteRequest,
    ProductBulkDeleteResult,
    ProductDeleteItemResult,
)
from app.api.v1.surface import OPS, route
from app.core.rbac import Principal
from app.services import product_delete

router = APIRouter()
CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]


@route(router, OPS["products_delete"])
async def products_delete(
    product_id: Annotated[UUID, PathParam()], principal: CurrentPrincipal
) -> Response:
    """Delete a product, and a style's sizes with it. Its open questions close, its queued jobs
    are cancelled (no AI is spent on it), its listings are archived and it leaves every list and
    the inbox; its photos are removed by the daily clean-up. 409 `INVALID_STATUS_TRANSITION`
    when a listing was sent to a marketplace (live, published or ever submitted), 409
    `SUBMISSION_IN_FLIGHT` while one is being sent, 409 `LISTING_BUSY` while one is generating;
    nothing is changed then and no marketplace is called. A size is 422 (delete its style). A
    second delete is 404, as for another tenant's product."""
    await product_delete.delete_product(principal, product_id)
    return Response(status_code=204)


@route(router, OPS["products_bulk_delete"])
async def products_bulk_delete(
    body: ProductBulkDeleteRequest,
    idempotency_key: IdempotencyKey,
    principal: CurrentPrincipal,
    request: Request,
) -> ProductBulkDeleteResult:
    """Delete up to 100 products under the rules of `DELETE /products/{product_id}`, each on its
    own: 200 with each item's `http_status` (204 deleted, 404, 409 or 422) and `error`. A
    refused product is left exactly as it was."""
    items = await product_delete.bulk_delete(
        principal,
        idempotency_key,
        request.url.path,
        body.model_dump_json().encode(),
        body.product_ids,
    )
    return ProductBulkDeleteResult(
        items=[
            ProductDeleteItemResult(
                product_id=i.product_id, http_status=i.http_status, error=i.error
            )
            for i in items
        ]
    )
