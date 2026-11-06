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
- SI1 D1c: a read also carries the intake fields (style number, money as 2-decimal strings,
  the fibre list, the size chart table, the size chart URL), the image details, a style's
  colourways and, on a single read, the effective manufacturer (own, else the style's, else the
  brand default, with `inherited`). `product_etag` is the version a PATCH sends back. `_view`
  stays total over stored rows: a malformed stored value reads as None, never an error.

Tenant and actor come only from the principal.
"""

from __future__ import annotations

import json
import math
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final, Literal

from mkos_schemas.codes import ErrorCode

from app.core.errors import InputError, NotFoundError
from app.core.rbac import Principal
from app.repositories.images import ImageDetail
from app.repositories.product_facts import manufacturer_inherited
from app.repositories.uow import TenantUnitOfWork, tenant_uow
from app.repositories.variants import ColourwayRow
from app.services.product_facts import AttributeValue, build_attributes, seller_provenance

__all__ = [
    "NewProduct",
    "PartyView",
    "ProductView",
    "create_product",
    "get_product",
    "list_products",
    "product_etag",
]

PRODUCT_ETAG_NS: Final = uuid.UUID("5d0f0b43-2a4e-4c55-9d47-6a3f3a1c8e21")
SIZE_CHART_UNITS: Final = frozenset({"cm", "in"})
SIZE_CHART_BASES: Final = frozenset({"garment", "body"})


def product_etag(product_id: uuid.UUID, updated_at: datetime) -> str:
    """The product's version, a quoted UUID (the `tenants.etag` form)."""
    return f'"{uuid.uuid5(PRODUCT_ETAG_NS, f"{product_id}:{updated_at.isoformat()}")}"'


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
class PartyView:
    id: uuid.UUID
    legal_name: str | None
    address_line1: str | None
    address_line2: str | None
    city: str | None
    state_code: str | None
    postal_code: str | None
    country_code: str
    gstin: str | None
    inherited: bool


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
    style_number: str | None = None
    mrp: str | None = None
    selling_price: str | None = None
    currency: str | None = None
    fiber_composition: list[dict[str, Any]] | None = None
    size_chart: dict[str, Any] | None = None
    size_chart_url: str | None = None
    manufacturer: PartyView | None = None
    image_details: tuple[ImageDetail, ...] = ()
    colourways: tuple[ColourwayRow, ...] = ()
    etag: str = ""


def _money(value: Any) -> str | None:
    return format(value, ".2f") if isinstance(value, Decimal) else None


def _normalized(facts: Mapping[str, Any], key: str) -> Any:
    fact = facts.get(key)
    return fact.get("normalized") if isinstance(fact, Mapping) else None


def _fibres(facts: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    stored = _normalized(facts, "fiber_composition")
    if not isinstance(stored, list) or not stored:
        return None
    out: list[dict[str, Any]] = []
    for item in stored:
        if not isinstance(item, Mapping):
            return None
        fibre, percent = item.get("fibre"), item.get("percent")
        if not isinstance(fibre, str) or isinstance(percent, bool):
            return None
        if not isinstance(percent, int | float) or not math.isfinite(percent):
            return None
        out.append({"fibre": fibre, "percent": percent})
    return out


def _size_chart(facts: Mapping[str, Any]) -> dict[str, Any] | None:
    stored = _normalized(facts, "size_measurements")
    if not isinstance(stored, Mapping):
        return None
    rows = stored.get("rows")
    if stored.get("unit") not in SIZE_CHART_UNITS or stored.get("basis") not in SIZE_CHART_BASES:
        return None
    if not isinstance(rows, list) or not rows:
        return None
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("size"), str):
            return None
        kept: dict[str, Any] = {"size": row["size"]}
        for key, value in row.items():
            if key == "size":
                continue
            ok = isinstance(value, int | float) and not isinstance(value, bool)
            if not ok or not math.isfinite(value):
                return None
            kept[key] = value
        out.append(kept)
    return {"unit": stored["unit"], "basis": stored["basis"], "rows": out}


def _text_value(facts: Mapping[str, Any], key: str) -> str | None:
    fact = facts.get(key)
    value = fact.get("value") if isinstance(fact, Mapping) else None
    return value if isinstance(value, str) else None


def _party_view(party: Any, *, inherited: bool) -> PartyView:
    return PartyView(
        id=party.id,
        legal_name=party.legal_name,
        address_line1=party.address_line1,
        address_line2=party.address_line2,
        city=party.city,
        state_code=party.state_code,
        postal_code=party.postal_code,
        country_code=party.country_code,
        gstin=party.gstin,
        inherited=inherited,
    )


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


def _view(
    row: Any,
    images: list[str] | None = None,
    *,
    details: tuple[ImageDetail, ...] = (),
    party: PartyView | None = None,
    colourways: tuple[ColourwayRow, ...] = (),
) -> ProductView:
    """Total over every row the DB CHECKs accept: nothing here re-applies input rules. `images`
    are the product's live image keys in slot order (S17b CP7)."""
    facts: Mapping[str, Any] = row.attributes or {}
    if not isinstance(facts, Mapping):
        facts = {}
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
        style_number=row.style_number,
        mrp=_money(row.mrp),
        selling_price=_money(row.selling_price),
        currency=row.currency,
        fiber_composition=_fibres(facts),
        size_chart=_size_chart(facts),
        size_chart_url=_text_value(facts, "size_chart_url"),
        manufacturer=party,
        image_details=details,
        colourways=colourways,
        etag=product_etag(row.id, row.updated_at),
    )


async def _full_view(uow: TenantUnitOfWork, row: Any) -> ProductView:
    """A single read: the image details, a style's colourways and the effective manufacturer
    (own → style → brand default)."""
    style = None
    if row.parent_product_id is not None:
        style = await uow.products.get_by_id(row.parent_product_id)
    party_id = row.manufacturer_party_id
    if party_id is None and style is not None:
        party_id = style.manufacturer_party_id
    inherited = manufacturer_inherited(row, style)
    if party_id is None:
        party_id = await uow.brand_profiles.default_manufacturer()
    party_row = None if party_id is None else await uow.parties.get(party_id)
    party = None if party_row is None else _party_view(party_row, inherited=inherited)
    keys = await uow.images.live_keys_for_products([row.id])
    details = tuple(await uow.images.details_for_product(row.id))
    colourways: tuple[ColourwayRow, ...] = ()
    if row.variation_theme is not None and row.parent_product_id is None:
        colourways = tuple(await uow.colourways.colourways_of(row.id))
    return _view(row, keys[row.id], details=details, party=party, colourways=colourways)


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
        return await _full_view(uow, row)


async def list_products(
    principal: Principal, *, limit: int, after: tuple[datetime, uuid.UUID] | None
) -> tuple[list[ProductView], bool]:
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        rows, has_more = await uow.products.list_page(limit=limit, after=after)
        keys = await uow.images.live_keys_for_products([r.id for r in rows])  # one query a page
        return [_view(r, keys[r.id]) for r in rows], has_more
