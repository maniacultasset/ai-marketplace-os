"""Client feedback 11 Nov (item 3): the live SKU check (`GET /skus/availability`), the batch
check before a variants save or a bulk upload (`POST /skus/availability-check`) and "Make one
for me" (`POST /skus/suggestion`)."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from mkos_schemas.base import Slug, StrictModel, WireModel
from pydantic import Field, StringConstraints

from app.api.v1.schemas.products import Sku


class SkuAvailabilityOut(WireModel):
    """Whether the SKU is free in this brand. Styles and variants share one set of SKUs, and a
    deleted product's SKU stays taken. When it is taken by a product you can open, its id and
    name come with it."""

    sku: Sku
    available: bool
    product_id: UUID | None = None
    title: Annotated[str, Field(max_length=500)] | None = None
    is_variant: bool = False
    deleted: bool = False


# a NUL can never be stored (PostgreSQL text), so it is refused here rather than at the query
CheckedSku = Annotated[str, Field(pattern=r"^[^\]\s\x00]{1,64}$", examples=["ACME-KUR-0007"])]


class SkuBatchCheckIn(StrictModel):
    """The SKUs about to be sent: a variants grid's SKUs or a bulk upload's `seller_sku` cells.
    Repeats are allowed and checked once."""

    skus: Annotated[list[CheckedSku], Field(min_length=1, max_length=1000)]


class SkuBatchCheckOut(WireModel):
    """The SKUs already used in this brand, each once, in the order first sent (free SKUs are
    left out). Each says which product holds it, as `GET /skus/availability` does. Repeats
    within the request are not reported: the caller sees those itself."""

    checked: Annotated[int, Field(ge=0, le=1000)]
    taken: list[SkuAvailabilityOut]


class SkuSuggestionIn(StrictModel):
    """The category picked so far and the product's own brand name, when it has one (else the
    store's name is used)."""

    category: Slug | None = None
    brand_name: Annotated[str, StringConstraints(max_length=200)] | None = None


class SkuSuggestionOut(WireModel):
    """A new SKU `<BRAND>-<CATEGORY>-<NNNN>`, free in this brand now and never offered before.
    It is not reserved: save the product to keep it."""

    sku: Sku
