"""Products (CODE-PLAN S12 T7; PLAN D2-D5, CP1, CP2; BACKEND-ARCHITECTURE.md:1332-1333).

- `create_product`: refusals that need no database come first, before any transaction:
  a NUL character in `sku`, `title` or a string attribute value (PostgreSQL rejects it as 22021,
  which would be a 500) -> 422; no `category` -> 422 (`products.category` is NOT NULL,
  DATABASE.md:410, so a NULL category cannot be stored); any `images` key (no tenant image can exist before S17b, CP2) -> 404 `images[0]`. Then
  one `tenant_uow`: resolve the attribute keys, build the seller facts, insert the row, write the
  `product.created` audit row with the created facts. The SKU unique is the natural key: a
  duplicate or a lost-response retry is 409 `SKU_CONFLICT`.
- `get_product` / `list_products`: the caller's tenant only; another tenant's id reads as missing.
  `images` holds the product's live image keys in slot order (S17b CP7), one query per page;
  `images` on create stays 404 (S17b D11: images arrive through `/images/upload`).

Tenant and actor come only from the principal.
"""

from __future__ import annotations

import json
import math
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from mkos_schemas.codes import ErrorCode

from app.core.errors import InputError, NotFoundError
from app.core.rbac import Principal
from app.repositories.uow import tenant_uow
from app.services.product_facts import AttributeValue, build_attributes, seller_provenance

__all__ = ["NewProduct", "ProductView", "create_product", "get_product", "list_products"]

_NUL = "\x00"
FlatValue = str | bool | int | float
"""A wire scalar of the flat CP1 view (`ProductOut.attributes`)."""


@dataclass(frozen=True, slots=True)
class NewProduct:
    sku: str
    title: str
    category: str | None
    images: tuple[str, ...]
    attributes: Mapping[str, AttributeValue]


@dataclass(frozen=True, slots=True)
class ProductView:
    id: uuid.UUID
    sku: str
    title: str
    status: Literal["draft", "active"]
    category: str  # products.category is NOT NULL (DATABASE.md:410)
    attributes: dict[str, FlatValue]
    images: list[str]
    parent_product_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


def _flat(fact: Any) -> FlatValue | None:
    """The flat CP1 value of one stored Fact: `value`, else `normalized` (fact_map_valid allows a
    Fact with only `normalized`). None when neither is a wire scalar (a list or object a later
    writer stored): the flat view has no place for it, and one such Fact must not break the read."""
    if not isinstance(fact, Mapping):
        return None
    flat = fact["value"] if fact.get("value") is not None else fact.get("normalized")
    if isinstance(flat, float) and not math.isfinite(flat):
        return None
    return flat if isinstance(flat, str | bool | int | float) else None


def _view(row: Any, images: list[str] | None = None) -> ProductView:
    """Total over every row the DB CHECKs accept: nothing here re-applies input rules. `images`
    are the product's live image keys in slot order (S17b CP7)."""
    facts: Mapping[str, Any] = row.attributes or {}
    flat = {key: _flat(fact) for key, fact in facts.items()}
    return ProductView(
        id=row.id,
        sku=row.sku,
        title=row.title,
        status=row.status,
        category=row.category,
        # CP1: flat values under canonical keys; the envelopes come later as `facts`
        attributes={key: value for key, value in flat.items() if value is not None},
        images=list(images or []),
        parent_product_id=row.parent_product_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _refuse_nul(cmd: NewProduct) -> None:
    if _NUL in cmd.sku:
        raise InputError(ErrorCode.VALIDATION_FAILED, "sku")
    if _NUL in cmd.title:
        raise InputError(ErrorCode.VALIDATION_FAILED, "title")
    for key, value in cmd.attributes.items():
        if isinstance(value, str) and _NUL in value:
            raise InputError(ErrorCode.VALIDATION_FAILED, f"attributes.{key}")


async def create_product(principal: Principal, cmd: NewProduct) -> ProductView:
    _refuse_nul(cmd)
    if cmd.category is None:
        raise InputError(ErrorCode.VALIDATION_FAILED, "category")
    if cmd.images:
        raise NotFoundError("images[0]")
    now = datetime.now(UTC)
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        defs = await uow.attribute_definitions.resolve(cmd.attributes.keys())
        facts = build_attributes(cmd.attributes, defs, now=now)
        row = await uow.products.create(
            sku=cmd.sku,
            title=cmd.title,
            category=cmd.category,
            attributes=facts,
            provenance=seller_provenance(now=now),
            created_by=principal.user_id,
        )
        await uow.audit.write(
            tenant_id=principal.tenant_id,
            actor_type="user",
            actor_user_id=principal.user_id,
            action="product.created",
            entity_type="product",
            entity_id=row.id,
            after=json.dumps({"attributes": facts}),
        )
        return _view(row)


async def get_product(principal: Principal, product_id: uuid.UUID) -> ProductView:
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        row = await uow.products.get_by_id(product_id)
        if row is None:
            raise NotFoundError("product_id")
        keys = await uow.images.live_keys_for_products([row.id])
        return _view(row, keys[row.id])


async def list_products(
    principal: Principal, *, limit: int, after: tuple[datetime, uuid.UUID] | None
) -> tuple[list[ProductView], bool]:
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        rows, has_more = await uow.products.list_page(limit=limit, after=after)
        keys = await uow.images.live_keys_for_products([r.id for r in rows])  # one query a page
        return [_view(r, keys[r.id]) for r in rows], has_more
