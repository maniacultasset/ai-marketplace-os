"""Products (owner steps S12, S13). Create, list and read are S12's (CODE-PLAN S12 T9-T12): the
route maps the request onto the service and the view onto `ProductOut`; domain errors render
through the shared handlers (`app/api/errors.py`). The CSV import stays S13's stub."""

from __future__ import annotations

from typing import Annotated, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi import Path as PathParam

from app.api.errors import NotImplementedYet
from app.api.pagination import PageInfo, PageParams, encode_cursor
from app.api.v1.deps import IdempotencyKey, get_current_principal
from app.api.v1.schemas.products import ProductCreate, ProductOut, ProductPage
from app.api.v1.surface import OPS, route, stub
from app.core.rbac import Principal
from app.services import products

router = APIRouter()
CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]


def _out(view: products.ProductView) -> ProductOut:
    """A DB-sourced row is built, not validated (R1-F1): the DB CHECKs are the read-side truth, and
    `ProductOut`'s input patterns (`Sku`, the non-blank title, the slug) must not turn one stored row
    into a 500 for the whole tenant list. The service already keeps `attributes` to wire scalars."""
    return ProductOut.model_construct(
        id=view.id,
        sku=view.sku,
        title=view.title,
        status=view.status,
        category=view.category,
        attributes=view.attributes,
        images=view.images,
        parent_product_id=view.parent_product_id,
        created_at=view.created_at,
        updated_at=view.updated_at,
    )


@route(router, OPS["products_create"])
async def products_create(body: ProductCreate, principal: CurrentPrincipal) -> ProductOut:
    """Create one product. The SKU is unique per brand (409 SKU_CONFLICT); an unknown attribute
    key is 422 UNKNOWN_FACT_KEY."""
    view = await products.create_product(
        principal,
        products.NewProduct(
            sku=body.sku,
            title=body.title,
            category=body.category,
            images=tuple(body.images),
            attributes=dict(body.attributes),
        ),
    )
    return _out(view)


@route(router, OPS["products_list"])
async def products_list(
    page: Annotated[PageParams, Depends()], principal: CurrentPrincipal
) -> ProductPage:
    """List the brand's products, newest first."""
    views, has_more = await products.list_products(principal, limit=page.limit, after=page.after)
    last = views[-1] if views else None
    next_cursor = encode_cursor(last.created_at, last.id) if has_more and last else None
    return ProductPage(
        data=[_out(v) for v in views],
        page=PageInfo(next_cursor=next_cursor, has_more=has_more, limit=page.limit),
    )


@route(router, OPS["products_bulk"])
@stub
async def products_bulk(
    file: Annotated[UploadFile, File(description="The CSV file (multipart only, 10 MiB cap).")],
    idempotency_key: IdempotencyKey,
) -> NoReturn:
    """Start a CSV import: 202 with the import and its job. Bad rows are isolated, never failing
    the batch, and the import does not start generation."""
    raise NotImplementedYet("products_bulk")


@route(router, OPS["products_get"])
async def products_get(
    product_id: Annotated[UUID, PathParam()], principal: CurrentPrincipal
) -> ProductOut:
    """Read one product. Another brand's id is 404, never 403."""
    return _out(await products.get_product(principal, product_id))
