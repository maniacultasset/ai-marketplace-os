"""`PATCH /products/{product_id}` (SI1 T12; PLAN D1b, AC3-AC5; CP12-CP14).

SI5: `product_identifier` and `identifier_exemption` (`product_identifier.plan`); on a style the
exemption fans out to its live variants without a barcode (one audit row each), and the style row
itself is not written.

A JSON merge patch: an absent field is unchanged; `null` clears `style_number`, `mrp`,
`selling_price`, `size_chart`, `manufacturer` (back to the inherited party) and an attribute key
(only a seller-written Fact). `sku` is immutable. `category` changes only while the product has
no listing.

Order (CP13): lock the row, compute the change set (`plan_patch`, pure), and only then compare
`If-Match`. A body that changes nothing answers 200 with the current ETag whatever tag was sent,
so a lost-response retry is safe (invariant 10). Every write goes through `FactWriter`, then one
`product.fact_updated` audit row holds the changed keys' envelopes only, under `attributes` and
`provenance` (the allow-list; never a party's name or address, CP4).

SI3: `confirm_attributes` confirms photo-derived values as they are (`confirmed_by`/`_at` on the
`ai` Fact); `{attributes: {<key>: null}}` on a photo value is the reject. SI9 B5: the reject of an
unconfirmed photo value records the value in `products.photo_rejections[key]`, so the next photo
read never writes or asks it again; a seller value or a confirmation for the key clears it. The
same request re-validates the product's `validated|needs_review|approved` listings (skipping one
another transaction holds), so a listing that still uses the value is flagged at once. A seller write of a key
cancels any live fact question on it (`superseded`, CP-D11).

SI8: `brand_name` must be one of the tenant's own brand names (`products.checked_brand`, CP-D9;
422 VALUE_NOT_ALLOWED otherwise) and is stored in that name's spelling; `null` clears it back to
the brand default. `gender` is one of the six audiences (the schema's `Literal`).

SI12: `hsn_code`, `country_of_origin` and `package` are seller column facts under the dictionary
keys `hsn_code`, `country_of_origin`, `package_dimensions` and `package_weight`. `package` merges
(JSON merge patch, CP-D8): the three dimensions are one group (set together or cleared together),
`weight_g` stands alone, a key not sent is unchanged and `package: null` clears all four.

Levels: a style-owned field (`style_number`, `brand_name`, `gender`, `size_chart`, an attribute
outside the row's owner levels) on a variant is 422 `FACT_LEVEL_INVALID`. Money (CP14): a unit's
own or inherited
price is at most its own or inherited MRP. A style money change is checked against each live
child: a child that inherits the changed price and goes over its MRP is 422 `selling_price`,
any other child breach is 422 `mrp`.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

from mkos_schemas.codes import ErrorCode
from sqlalchemy.exc import DBAPIError

from app.core.errors import ConflictError, InputError, NotFoundError
from app.core.rbac import Principal
from app.repositories.attribute_definitions import AttributeDef
from app.repositories.uow import tenant_uow
from app.services import product_identifier
from app.services.intake_values import PartyInput
from app.services.listing_validate import validate_listing
from app.services.photo_facts import V1_KEYS
from app.services.product_facts import (
    SIZE_CHART_KEY,
    SIZE_CHART_LEVELS,
    STANDALONE,
    build_attributes,
    fact_stamp,
    seller_column_fact,
    size_chart_fact,
)
from app.services.products import (
    ProductView,
    checked_brand,
    full_view,
    party_fields,
    product_etag,
)

__all__ = ["ProductChange", "plan_patch", "update_product"]

PATCH_FIELDS: Final = frozenset(
    {
        "title",
        "category",
        "style_number",
        "brand_name",
        "gender",
        "mrp",
        "selling_price",
        "manufacturer",
        "size_chart",
        "attributes",
        "confirm_attributes",
        # SI5: the barcode and the GTIN exemption (`product_identifier.plan`)
        "product_identifier",
        "identifier_exemption",
        # SI12: the seller fields and the size-chart evidence
        "hsn_code",
        "country_of_origin",
        "package",
        "size_chart_image_id",
    }
)
_ALL_LEVELS: Final = frozenset({"style", "variant", "standalone"})
# typed body field → (products column, dictionary key, owner levels)
TYPED: Final[Mapping[str, tuple[str, str, frozenset[str]]]] = {
    "title": ("title", "title", frozenset({"style", "variant", "standalone"})),
    "category": ("category", "category", frozenset({"style", "variant", "standalone"})),
    "style_number": ("style_number", "style_number", frozenset({"style", "standalone"})),
    # SI8: the brand override and the audience are style-owned (`ck_products_style_owned`)
    "brand_name": ("brand_name", "brand_name", frozenset({"style", "standalone"})),
    "gender": ("gender", "gender", frozenset({"style", "standalone"})),
    "mrp": ("mrp", "mrp", frozenset({"style", "variant", "standalone"})),
    "selling_price": (
        "selling_price",
        "selling_price",
        frozenset({"style", "variant", "standalone"}),
    ),
    "manufacturer": (
        "manufacturer_party_id",
        "manufacturer",
        frozenset({"style", "variant", "standalone"}),
    ),
    # SI12 (owner levels from the dictionary: style, variant, standalone)
    "hsn_code": ("hsn_code", "hsn_code", _ALL_LEVELS),
    "country_of_origin": ("country_of_origin", "country_of_origin", _ALL_LEVELS),
}
# SI12: `package` (body key → products column); the dims are one dictionary key
PACKAGE_DIMS: Final[Mapping[str, str]] = {
    "length_cm": "package_length_cm",
    "breadth_cm": "package_breadth_cm",
    "height_cm": "package_height_cm",
}
PACKAGE_DIMS_KEY: Final = "package_dimensions"
PACKAGE_WEIGHT_KEY: Final = "package_weight"
PACKAGE_WEIGHT_COLUMN: Final = "package_weight_g"
_NOT_NULLABLE: Final = frozenset({"title", "category"})


@dataclass(frozen=True)
class ProductChange:
    """The planned writes. `typed_set` maps a products column to its new value; `typed_clear`
    names dictionary keys to clear; `attrs_set` maps an attribute key to its new Fact."""

    typed_set: dict[str, Any] = field(default_factory=dict)
    typed_clear: tuple[str, ...] = ()
    # SI12: dictionary key → {column: value} set together (the package dimensions)
    groups_set: dict[str, dict[str, Any]] = field(default_factory=dict)
    attrs_set: dict[str, dict[str, Any]] = field(default_factory=dict)
    attrs_clear: tuple[str, ...] = ()
    # SI3: (index in `confirm_attributes`, key) of the photo values to confirm
    confirm: tuple[tuple[int, str], ...] = ()

    @property
    def empty(self) -> bool:
        return not (
            self.typed_set
            or self.typed_clear
            or self.groups_set
            or self.attrs_set
            or self.attrs_clear
            or self.confirm
        )


def _level(row: Any) -> str:
    if row.parent_product_id is not None:
        return "variant"
    if row.variation_theme is not None:
        return "style"
    return STANDALONE


def is_photo_value(stored: Any) -> bool:
    """An unconfirmed photo-derived Fact (`ai`, `image:` evidence): what a reject marks."""
    return (
        isinstance(stored, Mapping)
        and stored.get("source") == "ai"
        and stored.get("confirmed_at") is None
        and str(stored.get("evidence_ref") or "").startswith("image:")
    )


def _same_fact(stored: Any, new: Mapping[str, Any], key: str | None = None) -> bool:
    """SI12: for the size chart the evidence counts too, so confirming an unchanged chart from a
    photo stores its `image:` evidence and a re-save without it drops a stale one."""
    if not isinstance(stored, Mapping):
        return False
    compared: tuple[str, ...] = ("value", "normalized", "source")
    if key == SIZE_CHART_KEY:
        compared = (*compared, "evidence_ref")
    return all(stored.get(k) == new.get(k) for k in compared)


def _plan_money(
    row: Any,
    style: Any,
    typed: Mapping[str, Any],
    cleared: set[str],
    children: Sequence[tuple[Decimal | None, Decimal | None]],
) -> None:
    def effective(column: str) -> Decimal | None:
        if column in typed:
            own: Decimal | None = typed[column]
        elif column in cleared:
            own = None
        else:
            own = getattr(row, column)
        if own is None and style is not None:
            inherited: Decimal | None = getattr(style, column)
            return inherited
        return own

    price, mrp = effective("selling_price"), effective("mrp")
    if price is not None and mrp is not None and price > mrp:
        changed_price = "selling_price" in typed or "selling_price" in cleared
        raise InputError(ErrorCode.VALIDATION_FAILED, "selling_price" if changed_price else "mrp")
    price_changed = "selling_price" in typed or "selling_price" in cleared
    if price_changed or "mrp" in typed or "mrp" in cleared:
        # a child's own value wins; a missing one inherits the style's new value
        for child_price, child_mrp in children:
            child_p = child_price if child_price is not None else price
            child_m = child_mrp if child_mrp is not None else mrp
            if child_p is not None and child_m is not None and child_p > child_m:
                inherits_price = child_price is None and price_changed
                raise InputError(
                    ErrorCode.VALIDATION_FAILED, "selling_price" if inherits_price else "mrp"
                )


def _plan_package(
    row: Any,
    package: Mapping[str, Any] | None,
    typed: dict[str, Any],
    cleared: list[str],
    groups: dict[str, dict[str, Any]],
) -> None:
    """SI12 (CP-D8): `package` as a JSON merge patch over the stored values."""
    dims_stored = any(getattr(row, c) is not None for c in PACKAGE_DIMS.values())
    weight_stored = getattr(row, PACKAGE_WEIGHT_COLUMN) is not None
    if package is None:
        if dims_stored:
            cleared.append(PACKAGE_DIMS_KEY)
        if weight_stored:
            cleared.append(PACKAGE_WEIGHT_KEY)
        return
    if any(name in package for name in PACKAGE_DIMS):
        values = {column: package.get(name) for name, column in PACKAGE_DIMS.items()}
        if all(v is None for v in values.values()):
            if dims_stored:
                cleared.append(PACKAGE_DIMS_KEY)
        elif any(v is None for v in values.values()):  # the wire model refuses it already
            raise InputError(ErrorCode.VALIDATION_FAILED, "package")
        elif any(getattr(row, c) != v for c, v in values.items()):
            groups[PACKAGE_DIMS_KEY] = values
    if "weight_g" in package:
        weight = package["weight_g"]
        if weight is None:
            if weight_stored:
                cleared.append(PACKAGE_WEIGHT_KEY)
        elif getattr(row, PACKAGE_WEIGHT_COLUMN) != weight:
            typed[PACKAGE_WEIGHT_COLUMN] = weight


def plan_patch(
    row: Any,
    style_row: Any,
    patch: Mapping[str, Any],
    defs: Sequence[AttributeDef],
    *,
    level: str,
    has_listing: bool,
    now: datetime,
    party_id: uuid.UUID | None = None,
    children: Sequence[tuple[Decimal | None, Decimal | None]] = (),
    size_chart_image_id: uuid.UUID | None = None,
) -> ProductChange:
    """The change set of `patch` against the locked `row` (pure). `party_id` is the matched or
    created party for a non-null `manufacturer`; `children` are a style's `(price, mrp)`."""
    unknown = set(patch) - PATCH_FIELDS
    if unknown:
        raise InputError(ErrorCode.VALIDATION_FAILED, sorted(unknown)[0])
    typed: dict[str, Any] = {}
    cleared: list[str] = []
    for name, (column, key, levels) in TYPED.items():
        if name not in patch:
            continue
        if level not in levels:
            raise InputError(ErrorCode.FACT_LEVEL_INVALID, name)
        value = patch[name]
        if value is None:
            if name in _NOT_NULLABLE:
                raise InputError(ErrorCode.VALIDATION_FAILED, name)
            if getattr(row, column) is not None:
                cleared.append(key)
            continue
        if name == "manufacturer":
            value = party_id
            if value is None:
                raise ValueError("a manufacturer patch needs its party id")
        if getattr(row, column) != value:
            if name == "category" and has_listing:
                raise InputError(ErrorCode.VALIDATION_FAILED, "category")
            typed[column] = value
    _plan_money(row, style_row, typed, set(cleared), children)
    groups: dict[str, dict[str, Any]] = {}
    if "package" in patch:
        _plan_package(row, patch["package"], typed, cleared, groups)

    stored: Mapping[str, Any] = row.attributes if isinstance(row.attributes, Mapping) else {}
    attrs_set: dict[str, dict[str, Any]] = {}
    attrs_clear: list[str] = []
    if "size_chart" in patch:
        if level not in SIZE_CHART_LEVELS:
            raise InputError(ErrorCode.FACT_LEVEL_INVALID, "size_chart")
        chart = patch["size_chart"]
        if chart is None:
            if SIZE_CHART_KEY in stored:
                attrs_clear.append(SIZE_CHART_KEY)
        else:
            fact = size_chart_fact(chart, now=now, image_id=size_chart_image_id)
            if not _same_fact(stored.get(SIZE_CHART_KEY), fact, SIZE_CHART_KEY):
                attrs_set[SIZE_CHART_KEY] = fact
    attributes: Mapping[str, Any] = patch.get("attributes") or {}
    values = {k: v for k, v in attributes.items() if v is not None}
    built = build_attributes(values, defs, now=now, level=level)
    if SIZE_CHART_KEY in built and "size_chart" in patch:
        raise InputError(ErrorCode.VALIDATION_FAILED, "size_chart")
    for key, fact in built.items():
        if not _same_fact(stored.get(key), fact):
            attrs_set[key] = fact
    for name in (k for k, v in attributes.items() if v is None):
        definition = next((d for d in defs if d.key == name or name in d.input_aliases), None)
        if definition is None:
            raise InputError(ErrorCode.UNKNOWN_FACT_KEY, f"attributes.{name}")
        if definition.storage != "attributes":
            raise InputError(ErrorCode.VALIDATION_FAILED, f"attributes.{name}")
        if level not in definition.owner_levels:
            raise InputError(ErrorCode.FACT_LEVEL_INVALID, f"attributes.{name}")
        if definition.key in built or definition.key in attrs_clear:
            raise InputError(ErrorCode.VALIDATION_FAILED, f"attributes.{definition.key}")
        if definition.key in stored:
            attrs_clear.append(definition.key)
    return ProductChange(
        typed_set=typed,
        typed_clear=tuple(cleared),
        groups_set=groups,
        attrs_set=attrs_set,
        attrs_clear=tuple(attrs_clear),
        confirm=_plan_confirm(patch, stored, defs, attributes),
    )


def _plan_confirm(
    patch: Mapping[str, Any],
    stored: Mapping[str, Any],
    defs: Sequence[AttributeDef],
    attributes: Mapping[str, Any],
) -> tuple[tuple[int, str], ...]:
    """SI3: the photo values to confirm. A key also sent in `attributes`, or one that holds no
    photo value, is 422 `VALIDATION_FAILED`; an unknown key 422 `UNKNOWN_FACT_KEY`; both at
    `confirm_attributes[<i>]`. An already confirmed value is no change."""
    out: list[tuple[int, str]] = []
    for i, name in enumerate(patch.get("confirm_attributes") or ()):
        at = f"confirm_attributes[{i}]"
        definition = next((d for d in defs if d.key == name or name in d.input_aliases), None)
        if definition is None:
            raise InputError(ErrorCode.UNKNOWN_FACT_KEY, at)
        key = definition.key
        sent = {d.key for d in defs for n in attributes if d.key == n or n in d.input_aliases}
        if key in sent or name in attributes:
            raise InputError(ErrorCode.VALIDATION_FAILED, at)
        fact = stored.get(key)
        ref = fact.get("evidence_ref") if isinstance(fact, Mapping) else None
        if not (
            isinstance(fact, Mapping)
            and fact.get("source") == "ai"
            and isinstance(ref, str)
            and ref.startswith("image:")
        ):
            raise InputError(ErrorCode.VALIDATION_FAILED, at)
        if fact.get("confirmed_at") is None:
            out.append((i, key))
    return tuple(out)


def _envelopes(row: Any, change: ProductChange, column_keys: Mapping[str, str]) -> dict[str, Any]:
    """The stored envelopes of the changed keys (the audit `before`)."""
    attributes: Mapping[str, Any] = row.attributes or {}
    provenance: Mapping[str, Any] = row.provenance or {}
    attr_keys = [*change.attrs_set, *change.attrs_clear, *(k for _i, k in change.confirm)]
    prov_keys = [column_keys[c] for c in change.typed_set] + list(change.typed_clear)
    prov_keys += list(change.groups_set)
    return {
        "attributes": {k: attributes.get(k) for k in attr_keys},
        "provenance": {k: provenance.get(k) for k in prov_keys},
    }


async def _apply_fan_out(
    uow: Any, principal: Principal, fan_out: Sequence[tuple[uuid.UUID, Any, Any]], fact: Any
) -> None:
    """SI5 (T11): each changed variant written, with its own `product.fact_updated` row."""
    key = product_identifier.IDENTIFIER_KEY
    for variant_id, id_change, before in fan_out:
        await product_identifier.apply(uow.facts, variant_id, id_change, fact)
        await uow.audit.write(
            tenant_id=principal.tenant_id,
            actor_type="user",
            actor_user_id=principal.user_id,
            action="product.fact_updated",
            entity_type="product",
            entity_id=variant_id,
            before=json.dumps({"attributes": {}, "provenance": {key: before}}, sort_keys=True),
            after=json.dumps(
                {"attributes": {}, "provenance": {key: None if id_change.clears else fact}},
                sort_keys=True,
            ),
        )


async def update_product(
    principal: Principal, product_id: uuid.UUID, patch: Mapping[str, Any], if_match: str
) -> ProductView:
    """SI5: a barcode used by another live product of the tenant (the pre-check, or the unique
    index after the transaction rolled back) is 422 at `product_identifier.value`."""
    try:
        return await _update_product(principal, product_id, patch, if_match)
    except (DBAPIError, ConflictError) as exc:
        if product_identifier.is_duplicate(exc):
            raise product_identifier.duplicate() from None
        raise


async def _size_chart_image(
    uow: Any, row: Any, level: str, patch: Mapping[str, Any]
) -> uuid.UUID | None:
    """SI12: `size_chart_image_id` names a live `size_chart` image of this product, else 422 at
    `size_chart_image_id` (a foreign, deleted or other product's image alike: no oracle). On a
    variant the size chart's own level rule answers first (`plan_patch`)."""
    image_id = patch.get("size_chart_image_id")
    if image_id is None:
        return None
    if patch.get("size_chart") is None:  # the wire model refuses it already
        raise InputError(ErrorCode.VALIDATION_FAILED, "size_chart_image_id")
    if level not in SIZE_CHART_LEVELS:
        return None
    found = await uow.images.size_chart_of(row.id, image_id)
    if found is None:
        raise InputError(ErrorCode.VALIDATION_FAILED, "size_chart_image_id")
    image: uuid.UUID = found.id
    return image


async def _update_product(
    principal: Principal, product_id: uuid.UUID, patch: Mapping[str, Any], if_match: str
) -> ProductView:
    now = datetime.now(UTC)
    column_fact = seller_column_fact(now=now)
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        row = await uow.products.get_for_update(product_id)
        if row is None:
            raise NotFoundError("product_id")
        level = _level(row)
        style = None
        if row.parent_product_id is not None:
            style = await uow.products.get_by_id(row.parent_product_id)
        children: list[tuple[Decimal | None, Decimal | None]] = []
        if level == "style" and ("mrp" in patch or "selling_price" in patch):
            children = await uow.products.child_prices(row.id)
        has_listing = False
        if patch.get("category") is not None and patch["category"] != row.category:
            has_listing = await uow.listings.any_for_product(row.id)
        attribute_names = list((patch.get("attributes") or {}).keys())
        attribute_names += list(patch.get("confirm_attributes") or ())
        defs = await uow.attribute_definitions.resolve(attribute_names)
        party_id = None
        manufacturer = patch.get("manufacturer")
        if isinstance(manufacturer, PartyInput):
            if level not in TYPED["manufacturer"][2]:
                raise InputError(ErrorCode.FACT_LEVEL_INVALID, "manufacturer")
            # decision 16: a product route links the matching party, never rewrites it
            party_id = await uow.parties.match_or_create(party_fields(manufacturer))
        if patch.get("brand_name") is not None:
            if level not in TYPED["brand_name"][2]:
                raise InputError(ErrorCode.FACT_LEVEL_INVALID, "brand_name")
            # SI8 CP-D9: one of the tenant's own brand names, stored in that name's spelling
            patch = {**patch, "brand_name": await checked_brand(uow, patch["brand_name"])}
        chart_image = await _size_chart_image(uow, row, level, patch)
        change = plan_patch(
            row,
            style,
            patch,
            defs,
            level=level,
            has_listing=has_listing,
            now=now,
            party_id=party_id,
            children=children,
            size_chart_image_id=chart_image,
        )
        id_change = product_identifier.plan(
            level,
            row,
            identifier=patch.get("product_identifier"),
            exemption=patch.get("identifier_exemption"),
            sent=product_identifier.FIELDS & set(patch),
        )
        fan_out: list[tuple[uuid.UUID, Any, Any]] = []
        if level == "style" and patch.get("identifier_exemption") is not None:
            states = await uow.products.child_identifier_states(row.id)
            fan_out = product_identifier.plan_fan_out(states, bool(patch["identifier_exemption"]))
        if change.empty and id_change is None and not fan_out:
            return await full_view(uow, row)
        if product_etag(row.id, row.updated_at) != if_match:
            raise ConflictError(ErrorCode.STALE_VERSION, "products_etag")
        await _apply_fan_out(uow, principal, fan_out, column_fact)
        if change.empty and id_change is None:
            return await full_view(uow, row)  # only the variants changed (CP-D11)
        column_keys: dict[str, str] = {}
        for column in change.typed_set:
            key = await uow.facts.column_key(column)
            assert key is not None  # noqa: S101 - TYPED names dictionary columns
            column_keys[column] = key
        before = _envelopes(row, change, column_keys)
        if id_change is not None:
            if id_change.value is not None and await uow.products.identifier_taken(
                id_change.value, row.id
            ):
                raise product_identifier.duplicate()
            before["provenance"][product_identifier.IDENTIFIER_KEY] = (row.provenance or {}).get(
                product_identifier.IDENTIFIER_KEY
            )
            await product_identifier.apply(uow.facts, row.id, id_change, column_fact)
        for column, value in change.typed_set.items():
            await uow.facts.set_typed(row.id, column, value, column_fact)
        for values in change.groups_set.values():
            await uow.facts.set_typed_group(row.id, values, column_fact)
        for key in change.typed_clear:
            await uow.facts.clear_typed(row.id, key)
        for key, fact in change.attrs_set.items():
            await uow.facts.set_attribute(row.id, key, fact)
        rejected: list[str] = []
        for key in change.attrs_clear:
            # SI9 B5: the reject of an unconfirmed photo value keeps a per-key marker
            if (
                key in V1_KEYS
                and is_photo_value((row.attributes or {}).get(key))
                and await uow.facts.reject_photo_attribute(row.id, key) is not None
            ):
                rejected.append(key)
                continue
            await uow.facts.clear_attribute(row.id, key)
        stamp = fact_stamp(now)
        confirmed: dict[str, Any] = {}
        for i, key in change.confirm:
            await uow.facts.confirm_attribute(
                row.id, key, user_id=principal.user_id, at=stamp, field=f"confirm_attributes[{i}]"
            )
            stored_fact = (row.attributes or {}).get(key) or {}
            confirmed[key] = {
                **stored_fact,
                "confirmed_by": str(principal.user_id),
                "confirmed_at": stamp,
                "updated_at": stamp,
            }
        if change.attrs_set:
            await uow.human_tasks.cancel_fact_tasks(
                row.id, [f"facts.attributes.{k}" for k in change.attrs_set]
            )
        after: dict[str, Any] = {
            "attributes": {
                **dict(change.attrs_set),
                **dict.fromkeys(change.attrs_clear),
                **confirmed,
            },
            "provenance": {
                **{column_keys[c]: column_fact for c in change.typed_set},
                **dict.fromkeys(change.groups_set, column_fact),
                **dict.fromkeys(change.typed_clear),
            },
        }
        if id_change is not None:
            after["provenance"][product_identifier.IDENTIFIER_KEY] = (
                None if id_change.clears else column_fact
            )
        await uow.audit.write(
            tenant_id=principal.tenant_id,
            actor_type="user",
            actor_user_id=principal.user_id,
            action="product.fact_updated",
            entity_type="product",
            entity_id=row.id,
            before=json.dumps(before, sort_keys=True),
            after=json.dumps(after, sort_keys=True),
        )
        await uow.session.refresh(row)
        if rejected:
            # SI9 B5 (D8): the listings that may still use the rejected value are re-checked in
            # this request (after the refresh, so the check reads the facts as written), and the
            # review screen shows "facts changed, regenerate" at once
            for listing in await uow.listings.reviewable_for_product(row.id):
                await validate_listing(uow, principal, listing.id, None, on_reject=True)
        return await full_view(uow, row)
