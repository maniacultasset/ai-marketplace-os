"""Client feedback 11 Nov (item 3): the live SKU check (`GET /skus/availability`) and "Make one
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


class SkuSuggestionIn(StrictModel):
    """The category picked so far and the product's own brand name, when it has one (else the
    store's name is used)."""

    category: Slug | None = None
    brand_name: Annotated[str, StringConstraints(max_length=200)] | None = None


class SkuSuggestionOut(WireModel):
    """A new SKU `<BRAND>-<CATEGORY>-<NNNN>`, free in this brand now and never offered before.
    It is not reserved: save the product to keep it."""

    sku: Sku
