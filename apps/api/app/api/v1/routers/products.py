"""Product stubs (owner steps S12, S13)."""

from __future__ import annotations

from typing import Annotated, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi import Path as PathParam

from app.api.errors import NotImplementedYet
from app.api.pagination import PageParams
from app.api.v1.deps import IdempotencyKey
from app.api.v1.schemas.products import ProductCreate
from app.api.v1.surface import OPS, route, stub

router = APIRouter()


@route(router, OPS["products_create"])
@stub
async def products_create(body: ProductCreate) -> NoReturn:
    """Create one product. The SKU is unique per brand (409 SKU_CONFLICT); an unknown attribute
    key is 422 UNKNOWN_FACT_KEY."""
    raise NotImplementedYet("products_create")


@route(router, OPS["products_list"])
@stub
async def products_list(page: Annotated[PageParams, Depends()]) -> NoReturn:
    """List the brand's products, newest first."""
    raise NotImplementedYet("products_list")


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
@stub
async def products_get(product_id: Annotated[UUID, PathParam()]) -> NoReturn:
    """Read one product. Another brand's id is 404, never 403."""
    raise NotImplementedYet("products_get")
