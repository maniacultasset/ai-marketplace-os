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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final, Literal

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ImageSlot
from sqlalchemy.exc import DBAPIError

from app.core.config import get_settings
from app.core.errors import ConflictError, InputError, NotFoundError
from app.core.rbac import Principal
from app.core.resolved_facts import match_brand
from app.repositories.images import ImageDetail, MainImageRow
from app.repositories.jobs import PhotoReadRow
from app.repositories.parties import PartyFields
from app.repositories.product_facts import manufacturer_inherited
from app.repositories.uow import TenantUnitOfWork, tenant_uow
from app.repositories.variants import ColourwayRow
from app.services import image_views, product_identifier
from app.services.field_sources import FieldSource, field_sources
from app.services.hsn_suggest import HsnSuggestion, suggest_hsn
from app.services.image_fixes import fixes_for
from app.services.images import CreateUpload, check_create_uploads, register_on_create
from app.services.intake_values import PartyInput, check_price_le_mrp
from app.services.product_facts import (
    SIZE_CHART_KEY,
    STANDALONE,
    AttributeValue,
    build_attributes,
    seller_column_fact,
    seller_provenance,
    size_chart_fact,
)

__all__ = [
    "NewProduct",
    "PartyView",
    "ProductView",
    "create_product",
    "fibres_view",
    "get_product",
    "list_products",
    "product_etag",
    "size_chart_view",
]

PRODUCT_PK: Final = "pk_products"
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
    style_number: str | None = None
    brand_name: str | None = None
    gender: str | None = None
    mrp: Decimal | None = None
    selling_price: Decimal | None = None
    manufacturer: PartyInput | None = None
    size_chart: Mapping[str, Any] | None = None
    id: uuid.UUID | None = None
    image_uploads: tuple[CreateUpload, ...] = ()
    # SI5: the barcode `(type, value)` and the GTIN exemption
    product_identifier: tuple[str, str] | None = None
    identifier_exemption: bool | None = None
    # SI12: the seller fields; `package_dims` is (length, breadth, height) in cm
    hsn_code: str | None = None
    country_of_origin: str | None = None
    package_dims: tuple[Decimal, Decimal, Decimal] | None = None
    package_weight_g: int | None = None


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
class PhotoFactView:
    """SI3: an own-row attribute value read from the photos (`source ai`, `image:` evidence)."""

    key: str
    value: FlatValue
    confidence: float
    image_id: uuid.UUID
    confirmed: bool
    confirmed_at: datetime | None


def photo_facts_of(facts: Mapping[str, Any]) -> tuple[PhotoFactView, ...]:
    """The photo facts of a stored `attributes` map, by key; a ref that does not parse is
    skipped (the DB pattern allows more than a UUID)."""
    out: list[PhotoFactView] = []
    for key in sorted(facts):
        fact = facts[key]
        if not isinstance(fact, Mapping) or fact.get("source") != "ai":
            continue
        ref = fact.get("evidence_ref")
        if not isinstance(ref, str) or not ref.startswith("image:"):
            continue
        try:
            image_id = uuid.UUID(ref.removeprefix("image:"))
        except ValueError:
            continue
        value = _flat(fact)
        confidence = fact.get("confidence")
        if value is None or not isinstance(confidence, int | float):
            continue
        at = fact.get("confirmed_at")
        confirmed_at = None
        if isinstance(at, str):
            try:
                confirmed_at = datetime.fromisoformat(at)
            except ValueError:
                confirmed_at = None
        out.append(
            PhotoFactView(
                key=key,
                value=value,
                confidence=float(confidence),
                image_id=image_id,
                confirmed=at is not None,
                confirmed_at=confirmed_at,
            )
        )
    return tuple(out)


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
    brand_name: str | None = None
    brand_name_inherited: bool = False
    gender: str | None = None
    variant_attributes: dict[str, str] = field(default_factory=dict)
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
    photo_facts: tuple[PhotoFactView, ...] = ()
    photo_read: PhotoReadRow | None = None  # SI9 B7: the detail read only
    product_identifier: tuple[str, str] | None = None
    identifier_exemption: bool = False
    # SI12: the seller fields (single reads: own → style, origin → brand default; lists: own)
    hsn_code: str | None = None
    package: dict[str, Any] | None = None
    country_of_origin: str | None = None
    # SI12: detail reads only (`{}` and None on lists)
    field_sources: dict[str, FieldSource] = field(default_factory=dict)
    hsn_suggestion: HsnSuggestion | None = None
    size_chart_suggestion: dict[str, Any] | None = None
    size_chart_read_enabled: bool = False
    # the main photo's signed view (lists and single reads; `image_views`)
    thumbnail_url: str | None = None
    thumbnail_expires_at: datetime | None = None


READ_STATUSES: Final = frozenset({"read", "unreadable", "failed", "unavailable"})
PACKAGE_COLUMNS: Final = {
    "length_cm": "package_length_cm",
    "breadth_cm": "package_breadth_cm",
    "height_cm": "package_height_cm",
}


@dataclass(frozen=True, slots=True)
class SellerValues:
    """SI12: the effective seller fields of one read, and which came from elsewhere."""

    hsn_code: str | None = None
    package: dict[str, Any] | None = None
    country_of_origin: str | None = None
    hsn_from_style: bool = False
    dims_from_style: bool = False
    weight_from_style: bool = False
    country_from_style: bool = False
    country_from_brand: bool = False


# SI12: the typed dictionary keys the source chips cover → their products columns
SOURCE_COLUMNS: Final[Mapping[str, tuple[str, ...]]] = {
    "brand_name": ("brand_name",),
    "category": ("category",),
    "gender": ("gender",),
    "mrp": ("mrp",),
    "selling_price": ("selling_price",),
    "hsn_code": ("hsn_code",),
    "country_of_origin": ("country_of_origin",),
    "manufacturer": ("manufacturer_party_id",),
    "package_dimensions": ("package_length_cm", "package_breadth_cm", "package_height_cm"),
    "package_weight": ("package_weight_g",),
}


def _column_envelope(row: Any, key: str) -> dict[str, Any] | None:
    """The row's provenance of a typed key, with a `value` marker when the row holds a value."""
    if row is None or not any(getattr(row, c, None) is not None for c in SOURCE_COLUMNS[key]):
        return None
    provenance = row.provenance if isinstance(row.provenance, Mapping) else {}
    stored = provenance.get(key)
    return {**stored, "value": True} if isinstance(stored, Mapping) else None


def effective_envelopes(row: Any, style: Any) -> dict[str, Any]:
    """The Fact envelopes behind a single read's values: own, else the style's."""
    out: dict[str, Any] = {}
    for layer in (style, row):  # the row wins
        if layer is None:
            continue
        attributes = layer.attributes if isinstance(layer.attributes, Mapping) else {}
        out.update({k: v for k, v in attributes.items() if isinstance(v, Mapping)})
    for key, columns in SOURCE_COLUMNS.items():
        own_set = any(getattr(row, c, None) is not None for c in columns)
        envelope = _column_envelope(row if own_set else style, key)
        if envelope is not None:
            out[key] = envelope
    return out


def _newest_chart(details: tuple[ImageDetail, ...]) -> ImageDetail | None:
    """The product's size chart (CP-D3): its newest live style-level `size_chart` image."""
    charts = [d for d in details if d.slot == "size_chart" and d.colourway_id is None]
    if not charts:
        return None
    return max(
        charts, key=lambda d: (d.created_at or datetime.min.replace(tzinfo=UTC), str(d.image_id))
    )


async def size_chart_suggestion(
    uow: TenantUnitOfWork, row: Any, details: tuple[ImageDetail, ...]
) -> dict[str, Any] | None:
    """SI12: the read of the live size-chart photo (a style or standalone; else None). A stored
    result counts only for the photo's current clean copy; without one, a live read job shows
    `queued` or `reading` (one indexed read, CP-D6); otherwise None."""
    if row.parent_product_id is not None:
        return None
    chart = _newest_chart(details)
    if chart is None:
        return None
    stored = chart.size_chart_read
    if (
        stored is not None
        and chart.clean_content_hash is not None
        and stored.get("source_hash") == chart.clean_content_hash
        and stored.get("status") in READ_STATUSES
    ):
        confidence = stored.get("confidence")
        read_at = stored.get("read_at")
        is_read = stored.get("status") == "read"
        line = stored.get("unit_source_line") if is_read else None
        return {
            "image_id": chart.image_id,
            "status": stored["status"],
            "chart": stored.get("chart") if is_read else None,
            "unit_source_line": line if isinstance(line, str) else None,
            "confidence": confidence if isinstance(confidence, int | float) else None,
            "read_at": datetime.fromisoformat(read_at) if isinstance(read_at, str) else None,
        }
    job = await uow.job_runs.latest_for_entity("size_chart_read", chart.image_id)
    if job is None or job.status not in ("queued", "running"):
        return None
    status = "queued" if job.status == "queued" else "reading"
    return {"image_id": chart.image_id, "status": status, "chart": None}


def _own_dims(row: Any) -> dict[str, Any] | None:
    dims = {name: getattr(row, column, None) for name, column in PACKAGE_COLUMNS.items()}
    return dims if all(v is not None for v in dims.values()) else None


def seller_values(row: Any, style: Any = None, brand_country: str | None = None) -> SellerValues:
    """Own values; on a single read a missing one is the style's (the dims as one group), and a
    missing origin is then the brand default."""
    hsn = getattr(row, "hsn_code", None)
    hsn_from_style = hsn is None and getattr(style, "hsn_code", None) is not None
    if hsn_from_style:
        hsn = style.hsn_code
    dims = _own_dims(row)
    dims_from_style = dims is None and style is not None and _own_dims(style) is not None
    if dims_from_style:
        dims = _own_dims(style)
    weight = getattr(row, "package_weight_g", None)
    weight_from_style = weight is None and getattr(style, "package_weight_g", None) is not None
    if weight_from_style:
        weight = style.package_weight_g
    country = getattr(row, "country_of_origin", None)
    country_from_style = country is None and getattr(style, "country_of_origin", None) is not None
    if country_from_style:
        country = style.country_of_origin
    country_from_brand = country is None and brand_country is not None
    if country_from_brand:
        country = brand_country
    package: dict[str, Any] | None = None
    if dims is not None or weight is not None:
        package = {**(dims or dict.fromkeys(PACKAGE_COLUMNS)), "weight_g": weight}
    return SellerValues(
        hsn_code=hsn,
        package=package,
        country_of_origin=country,
        hsn_from_style=hsn_from_style,
        dims_from_style=dims_from_style,
        weight_from_style=weight_from_style,
        country_from_style=country_from_style,
        country_from_brand=country_from_brand,
    )


def _money(value: Any) -> str | None:
    return format(value, ".2f") if isinstance(value, Decimal) else None


def _normalized(facts: Mapping[str, Any], key: str) -> Any:
    fact = facts.get(key)
    return fact.get("normalized") if isinstance(fact, Mapping) else None


def fibres_view(facts: Mapping[str, Any]) -> list[dict[str, Any]] | None:
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


def size_chart_view(facts: Mapping[str, Any]) -> dict[str, Any] | None:
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


def party_view(party: Any, *, inherited: bool) -> PartyView:
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
    brand: tuple[str | None, bool] | None = None,
    gender: str | None = None,
    photo_read: PhotoReadRow | None = None,
    style_exempt: bool = False,
    seller: SellerValues | None = None,
) -> ProductView:
    """Total over every row the DB CHECKs accept: nothing here re-applies input rules. `images`
    are the product's live image keys in slot order (S17b CP7). SI5: a variant or standalone
    reads its own barcode columns; a style holds none and takes `style_exempt` (every live
    variant exempt, `style_exemptions`)."""
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
        brand_name=row.brand_name if brand is None else brand[0],
        brand_name_inherited=False if brand is None else brand[1],
        gender=row.gender if gender is None else gender,
        variant_attributes={
            str(k): str(v) for k, v in (row.variant_attributes or {}).items() if v is not None
        },
        mrp=_money(row.mrp),
        selling_price=_money(row.selling_price),
        currency=row.currency,
        fiber_composition=fibres_view(facts),
        size_chart=size_chart_view(facts),
        size_chart_url=_text_value(facts, "size_chart_url"),
        manufacturer=party,
        image_details=details,
        colourways=colourways,
        etag=product_etag(row.id, row.updated_at),
        photo_facts=photo_facts_of(facts),
        photo_read=photo_read,
        **_identifier_view(row, style_exempt),
        **_seller_view(row if seller is None else None, seller),
    )


def _seller_view(row: Any, seller: SellerValues | None) -> dict[str, Any]:
    values = seller if seller is not None else seller_values(row)
    return {
        "hsn_code": values.hsn_code,
        "package": values.package,
        "country_of_origin": values.country_of_origin,
    }


def _is_style(row: Any) -> bool:
    return row.parent_product_id is None and row.variation_theme is not None


def _identifier_view(row: Any, style_exempt: bool) -> dict[str, Any]:
    if _is_style(row):
        return {"product_identifier": None, "identifier_exemption": style_exempt}
    id_type, value = getattr(row, "identifier_type", None), getattr(row, "identifier_value", None)
    status = getattr(row, "identifier_status", None)
    pair = (id_type, value) if status == "provided" and id_type and value else None
    return {"product_identifier": pair, "identifier_exemption": status == "exempt"}


async def full_view(uow: TenantUnitOfWork, row: Any) -> ProductView:
    """A single read: the image details, a style's colourways, the effective manufacturer
    (own → style → brand default) and (SI8) the effective brand (own → style → the brand
    default, `brand_name_inherited` then true) and gender (own → style)."""
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
    party = None if party_row is None else party_view(party_row, inherited=inherited)
    keys = await uow.images.live_keys_for_products([row.id])
    edit_enabled = get_settings().image_edit_enabled
    details = tuple(
        replace(
            detail,
            fixes=tuple(
                fixes_for(detail.slot, detail.report, detail.border, edit_enabled=edit_enabled)
            ),
        )
        for detail in await uow.images.details_for_product(row.id)
    )
    details = await image_views.with_views(row.tenant_id, row.id, details)
    colourways: tuple[ColourwayRow, ...] = ()
    if row.variation_theme is not None and row.parent_product_id is None:
        colourways = tuple(await uow.colourways.colourways_of(row.id))
    brand_name = (
        row.brand_name if row.brand_name is not None else getattr(style, "brand_name", None)
    )
    brand_inherited = False
    if brand_name is None:
        brand_name = await uow.brand_profile.default_brand_name()
        brand_inherited = brand_name is not None
    gender = row.gender if row.gender is not None else getattr(style, "gender", None)
    photo_read = await uow.job_runs.latest_photo_read(row.id)
    exempt = False
    if _is_style(row):
        exempt = (await uow.products.style_exemptions([row.id]))[row.id]
    brand_country = None
    if row.country_of_origin is None and getattr(style, "country_of_origin", None) is None:
        brand_country = await uow.brand_profiles.default_country()
    seller = seller_values(row, style, brand_country)
    sources = field_sources(
        effective_envelopes(row, style),
        inherited={
            "manufacturer_inherited": inherited and party is not None,
            "brand_name_inherited": brand_inherited,
            "country_of_origin_from_brand": seller.country_from_brand,
        },
    )
    view = _view(
        row,
        keys[row.id],
        details=details,
        party=party,
        colourways=colourways,
        brand=(brand_name, brand_inherited),
        gender=gender,
        photo_read=photo_read,
        style_exempt=exempt,
        seller=seller,
    )
    return replace(
        view,
        field_sources=sources,
        hsn_suggestion=suggest_hsn(row.category, seller.hsn_code),
        size_chart_suggestion=await size_chart_suggestion(uow, row, details),
        size_chart_read_enabled=get_settings().size_chart_read_enabled,
        **await _thumbnail(uow, row, details),
    )


async def _thumbnail(
    uow: TenantUnitOfWork, row: Any, details: tuple[ImageDetail, ...]
) -> dict[str, Any]:
    """A single read's `thumbnail_url`: its own main photo's view (style-level first), else, for
    a variant, the list's pick from its style."""
    mains = [d for d in details if d.slot == ImageSlot.MAIN.value and d.view_url is not None]
    mains.sort(key=lambda d: d.colourway_id is not None)
    if mains:
        return {
            "thumbnail_url": mains[0].view_url,
            "thumbnail_expires_at": mains[0].view_expires_at,
        }
    if row.parent_product_id is None or details:
        return {}
    views = await _main_views(uow, row.tenant_id, [row])
    view = views.get(row.id)
    return (
        {} if view is None else {"thumbnail_url": view.url, "thumbnail_expires_at": view.expires_at}
    )


async def _main_views(
    uow: TenantUnitOfWork, tenant_id: uuid.UUID, rows: Sequence[Any]
) -> dict[uuid.UUID, image_views.ImageView]:
    """Per product, the signed view of its main photo (`image_views.pick_main`), two queries."""
    ids = {r.id for r in rows} | {r.parent_product_id for r in rows if r.parent_product_id}
    found = await uow.images.main_images_for_products(sorted(ids, key=str))
    picks: dict[uuid.UUID, MainImageRow] = {}
    for r in rows:
        colour = (r.variant_attributes or {}).get("color")
        pick = image_views.pick_main(
            found,
            product_id=r.id,
            parent_product_id=r.parent_product_id,
            colour=colour if isinstance(colour, str) else None,
        )
        if pick is not None:
            picks[r.id] = pick
    return await image_views.sign_mains(tenant_id, picks)


def _refuse_nul(cmd: NewProduct) -> None:
    if _NUL in cmd.sku:
        raise InputError(ErrorCode.VALIDATION_FAILED, "sku")
    if _NUL in cmd.title:
        raise InputError(ErrorCode.VALIDATION_FAILED, "title")
    for key, value in cmd.attributes.items():
        if isinstance(value, str) and _NUL in value:
            raise InputError(ErrorCode.VALIDATION_FAILED, f"attributes.{key}")
    if cmd.style_number is not None and _NUL in cmd.style_number:
        raise InputError(ErrorCode.VALIDATION_FAILED, "style_number")
    if cmd.brand_name is not None and _NUL in cmd.brand_name:
        raise InputError(ErrorCode.VALIDATION_FAILED, "brand_name")
    if cmd.manufacturer is not None and cmd.manufacturer.has_nul():
        raise InputError(ErrorCode.VALIDATION_FAILED, "manufacturer")


def party_fields(party: PartyInput, role: str = "manufacturer") -> PartyFields:
    return PartyFields(
        role=role,
        legal_name=party.legal_name,
        address_line1=party.address_line1,
        address_line2=party.address_line2,
        city=party.city,
        state_code=party.state_code,
        postal_code=party.postal_code,
        country_code=party.country_code,
        gstin=party.gstin,
        email=party.email,
        phone=party.phone,
    )


async def checked_brand(uow: TenantUnitOfWork, value: str | None) -> str | None:
    """SI8 CP-D9 (DATABASE.md:439, the Identity gate): a brand override is accepted only when it
    is one of the tenant's own brand names (its non-blank brand profile names or its tenant name,
    casefold after strip), and is stored in that name's spelling; else 422 VALUE_NOT_ALLOWED at
    `brand_name`. None stays None."""
    if value is None:
        return None
    matched = match_brand(value, await uow.brand_profile.brand_choices())
    if matched is None:
        raise InputError(ErrorCode.VALUE_NOT_ALLOWED, "brand_name")
    return matched


async def create_product(principal: Principal, cmd: NewProduct) -> ProductView:
    """SI5: a barcode already used in the tenant (the pre-check, or a concurrent create caught
    on `products_identifier_uq` after the transaction rolled back) is 422 at
    `product_identifier.value`."""
    try:
        return await _create_product(principal, cmd)
    except (DBAPIError, ConflictError) as exc:
        if product_identifier.is_duplicate(exc):
            raise product_identifier.duplicate() from None
        raise


async def _create_product(principal: Principal, cmd: NewProduct) -> ProductView:
    _refuse_nul(cmd)
    if cmd.category is None:
        raise InputError(ErrorCode.VALIDATION_FAILED, "category")
    if cmd.images:
        raise NotFoundError("images[0]")
    check_price_le_mrp(cmd.selling_price, cmd.mrp)
    if bool(cmd.image_uploads) != (cmd.id is not None):
        raise InputError(ErrorCode.VALIDATION_FAILED, "image_uploads")
    checked = []
    if cmd.id is not None:  # CP8: every key is checked before the transaction
        checked = await check_create_uploads(principal, cmd.id, cmd.image_uploads)
    sent = {
        name
        for name, value in (
            ("product_identifier", cmd.product_identifier),
            ("identifier_exemption", cmd.identifier_exemption),
        )
        if value is not None
    }
    id_change = product_identifier.plan(
        STANDALONE,
        None,
        identifier=cmd.product_identifier,
        exemption=cmd.identifier_exemption,
        sent=sent,
    )
    now = datetime.now(UTC)
    column_fact = seller_column_fact(now=now)
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        brand_name = await checked_brand(uow, cmd.brand_name)
        if (
            id_change is not None
            and id_change.value is not None
            and await uow.products.identifier_taken(id_change.value, None)
        ):
            raise product_identifier.duplicate()
        defs = await uow.attribute_definitions.resolve(cmd.attributes.keys())
        facts = build_attributes(cmd.attributes, defs, now=now)
        if cmd.size_chart is not None:
            if SIZE_CHART_KEY in facts:
                raise InputError(ErrorCode.VALIDATION_FAILED, "size_chart")
            facts[SIZE_CHART_KEY] = size_chart_fact(cmd.size_chart, now=now)
        provenance = seller_provenance(now=now)
        try:
            row = await uow.products.create(
                sku=cmd.sku,
                title=cmd.title,
                category=cmd.category,
                attributes=facts,
                provenance=provenance,
                created_by=principal.user_id,
                id=cmd.id,
            )
        except ConflictError as exc:
            # CP8: a live or soft-deleted product (any tenant) already holds the minted id
            if cmd.id is not None and exc.constraint == PRODUCT_PK:
                raise NotFoundError("image_uploads[0]") from None
            raise
        if checked:
            await register_on_create(uow, principal, row.id, checked)
        typed: dict[str, Any] = {
            "style_number": cmd.style_number,
            "brand_name": brand_name,
            "gender": cmd.gender,
            "mrp": cmd.mrp,
            "selling_price": cmd.selling_price,
            "hsn_code": cmd.hsn_code,
            "country_of_origin": cmd.country_of_origin,
            "package_weight_g": cmd.package_weight_g,
        }
        if cmd.manufacturer is not None:
            # decision 16: a product route links a matching party and never rewrites it
            typed["manufacturer_party_id"] = await uow.parties.match_or_create(
                party_fields(cmd.manufacturer)
            )
        for column, value in typed.items():
            if value is None:
                continue
            await uow.facts.set_typed(row.id, column, value, column_fact)
            key = await uow.facts.column_key(column)
            assert key is not None  # noqa: S101 - set_typed refused an unknown column
            provenance[key] = column_fact
        if cmd.package_dims is not None:  # SI12: the three dimensions are one fact
            length, breadth, height = cmd.package_dims
            await uow.facts.set_typed_group(
                row.id,
                {
                    "package_length_cm": length,
                    "package_breadth_cm": breadth,
                    "package_height_cm": height,
                },
                column_fact,
            )
            provenance["package_dimensions"] = column_fact
        if id_change is not None:
            await product_identifier.apply(uow.facts, row.id, id_change, column_fact)
            provenance[product_identifier.IDENTIFIER_KEY] = column_fact
        await uow.audit.write(
            tenant_id=principal.tenant_id,
            actor_type="user",
            actor_user_id=principal.user_id,
            action="product.created",
            entity_type="product",
            entity_id=row.id,
            after=json.dumps({"attributes": facts, "provenance": provenance}),
        )
        await uow.session.refresh(row)
        return await full_view(uow, row)


async def get_product(principal: Principal, product_id: uuid.UUID) -> ProductView:
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        row = await uow.products.get_by_id(product_id)
        if row is None:
            raise NotFoundError("product_id")
        return await full_view(uow, row)


async def list_products(
    principal: Principal, *, limit: int, after: tuple[datetime, uuid.UUID] | None
) -> tuple[list[ProductView], bool]:
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        rows, has_more = await uow.products.list_page(limit=limit, after=after)
        keys = await uow.images.live_keys_for_products([r.id for r in rows])  # one query a page
        exempt = await uow.products.style_exemptions([r.id for r in rows if _is_style(r)])
        thumbs = await _main_views(uow, principal.tenant_id, rows)
        views = [
            _with_thumb(_view(r, keys[r.id], style_exempt=exempt.get(r.id, False)), thumbs)
            for r in rows
        ]
        return views, has_more


def _with_thumb(
    view: ProductView, thumbs: Mapping[uuid.UUID, image_views.ImageView]
) -> ProductView:
    found = thumbs.get(view.id)
    if found is None:
        return view
    return replace(view, thumbnail_url=found.url, thumbnail_expires_at=found.expires_at)
