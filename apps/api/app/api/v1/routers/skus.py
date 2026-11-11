"""Client feedback 11 Nov (item 3, SKU ownership): the live SKU check (any member) and "Make one
for me" (`product:import`). The SKU is the seller SKU Kuinn sends to Amazon and Flipkart. Routes
import no `sqlalchemy` or `app.db`."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.api.v1.deps import IdempotencyKey, get_current_principal
from app.api.v1.schemas.skus import SkuAvailabilityOut, SkuSuggestionIn, SkuSuggestionOut
from app.api.v1.surface import OPS, route
from app.core.rbac import Principal
from app.services import sku_help

router = APIRouter()
CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]
SkuQuery = Annotated[
    str, Query(pattern=r"^[^\]\s]{1,64}$", max_length=64, examples=["ACME-KUR-0007"])
]


@route(router, OPS["skus_availability"])
async def skus_availability(sku: SkuQuery, principal: CurrentPrincipal) -> SkuAvailabilityOut:
    """Whether `sku` is free in this brand. Styles and variants share one set of SKUs, and a
    deleted product's SKU stays taken (`deleted: true`). A taken SKU names the product holding
    it. Another brand's SKUs never count."""
    found = await sku_help.check_sku(principal, sku)
    return SkuAvailabilityOut.model_validate(found, from_attributes=True)


@route(router, OPS["skus_suggest"])
async def skus_suggest(
    body: SkuSuggestionIn,
    idempotency_key: IdempotencyKey,
    principal: CurrentPrincipal,
    request: Request,
) -> SkuSuggestionOut:
    """A new SKU `<BRAND>-<CATEGORY>-<NNNN>` (the brand's first word, the category's short
    code, the next number for that pair). It is free in this brand now and was never offered
    before, so two people asking at once get different SKUs. A replay of the key answers the
    same SKU. It is not reserved: save the product to keep it. Variant SKUs are made from it."""
    _status, answer = await sku_help.suggest_sku(
        principal,
        idempotency_key,
        request.url.path,
        category=body.category,
        brand_name=body.brand_name,
    )
    return SkuSuggestionOut.model_validate(answer)
