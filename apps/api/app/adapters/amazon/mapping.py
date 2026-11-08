"""Canonical listing -> Amazon Listings Items payloads (spec L10533; invariant 4; S20 T6, T7).

Pure and deterministic: no HTTP, socket or database, and the same input always builds the same
payloads. The pinned category template's adapter-only columns drive it: `product_type`, each
spec's `marketplace_field` and `value_map` (DATABASE.md:541-542, T49).

- `item_payload(listing, unit, template, *, marketplace_id)`: one item's PUT body, the field
  paths it sends, the paths it skips (unsupported or unmapped, images past nine, an image with
  no locator) and its value errors.
  - Attribute values go as `[{value, marketplace_id[, language_tag]}]`.
  - An enum value is looked up in the spec's `value_map` (normalized: strip, casefold, NFKC),
    else passed through in the template's spelling when it is an allowed value, else
    VALUE_NOT_ALLOWED with the submitted value.
  - A measurement is `{value, unit}`. Images are locators from `listing.image_refs`.
- `check(listing)`: every error that blocks a create, so nothing is sent:
  - no mapped template -> CATEGORY_NOT_MAPPED (`listing`); a template with a NULL
    `product_type` or no `marketplace_field` at all is not mapped;
  - a required attribute missing or blank -> MISSING_ATTRIBUTE;
  - an unmappable enum -> VALUE_NOT_ALLOWED;
  - a bad family (C10) -> VARIATION_INVALID (`variants`);
  - a seller SKU `.` or `..` (C16) -> VALUE_NOT_ALLOWED;
  - SI5: a sellable item with no barcode and no GTIN exemption -> IDENTIFIER_MISSING (a create
    only; the update path and the preview of a live listing, `AdapterListing.live`, pass
    `require_identifier=False`, CP-D14).
- `family_payloads(listing)`: the parent first (content, theme, `parentage_level parent`), then
  each child (content, attributes with its variant values, images, the parent link).
- `preview(listing)`: what a create would send, per item, and the mapping report.
- `reverse_index(template)`: Amazon attribute -> the field paths that feed it, so a marketplace
  issue on `generic_keyword` fails both keyword paths.
- DW2: with a stored Product Type Definition (`listing.product_type_schema`) for the template's
  product type, `build()` also checks each item's attributes against it
  (`schema_check.check_item`). Those errors have their own dedupe, keyed on (field, code,
  raw_code, message), and a schema `required` the template check already reported is left out.
  A definition for another product type is not used (one SCHEMA_INVALID warning); none at all
  changes nothing (AC10).

- SI2: each sellable item (standalone, child) carries the seller's offer (`purchasable_offer`
  with `our_price` and `maximum_retail_price`, `condition_type new_new`) under requirements
  `LISTING`; a parent stays `LISTING_PRODUCT_ONLY`. The seller facts `country_of_origin`,
  `fiber_composition` and `manufacturer` (plus `manufacturer_contact_information`) go through
  the template spec of the same key, at `facts.<key>` (`variants[<sku>].facts.<key>` for a
  child). A missing price, MRP, manufacturer or country is MISSING_ATTRIBUTE; a price above the
  MRP is PRICE_ABOVE_MRP; any other currency than INR, or a price out of range, is
  VALUE_NOT_ALLOWED. Messages name the field only: the manufacturer's address is never echoed.
  An image locator must be an https URL (`locator_ok`; `http://` only when the listing allows
  it, the `image:<id>` tag only in a preview). An enum VALUE_NOT_ALLOWED suggests the nearest
  allowed values.

The adapter only adds errors; it never removes or downgrades a Rules Engine verdict (invariant
2): `required_attributes` and the enum vocabulary are the ones the engine checks.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

from mkos_schemas.codes import ERROR_CODE_META, ErrorCode
from mkos_schemas.enums import ErrorSource, Marketplace, RetryClass, Severity
from mkos_schemas.errors import ErrorItem, ErrorReport
from mkos_schemas.jobs import AttributeSpec, CategoryTemplate
from mkos_schemas.listing import AttributeValue, CanonicalListing
from mkos_schemas.paths import is_valid_field_path

from app.adapters.amazon import constants as c
from app.adapters.amazon import schema_check
from app.adapters.amazon.repairs import apply_repairs
from app.adapters.base import (
    FACT_SPEC_KEYS,
    PRICE_PATHS,
    AdapterItem,
    AdapterListing,
    AdapterPreview,
    AdapterTemplate,
    PreviewItem,
    SellerFacts,
    composition_text,
    fact_paths,
    locator_ok,
    nearest_values,
    submitted_fields,
)
from app.services.rules_engine.text import submitted_keywords

DEFAULT_MARKETPLACE_ID: Final = "A21TJRUUN4KGV"
UNSAFE_SKUS: Final = frozenset({".", ".."})
_RETRYABLE: Final = frozenset({RetryClass.AUTO_RETRY, RetryClass.AI_CORRECT})

Payload = dict[str, Any]


@dataclass(frozen=True)
class MappedItem:
    unit: AdapterItem
    payload: Payload
    submitted: tuple[str, ...]
    skipped: tuple[str, ...]
    errors: tuple[ErrorItem, ...] = ()
    paths: tuple[str, ...] = field(default=())
    """Every field path this item answers for: `submitted` + `skipped`, sorted."""
    repaired: tuple[tuple[UUID, str, bytes, bytes], ...] = ()
    """SI11: the repairs applied (id, field path, from digest, to digest)."""
    stale_repairs: tuple[UUID, ...] = ()
    """SI11: the repairs whose seller value changed since (retired by the publish job)."""


def normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def is_mapped(template: AdapterTemplate | None) -> bool:
    if template is None or not template.product_type:
        return False
    return any(spec.marketplace_field for spec in template.template.specs.values())


def error(
    path: str,
    code: ErrorCode,
    message: str,
    *,
    value: str | None = None,
    severity: Severity = Severity.ERROR,
    suggestion: str | None = None,
) -> ErrorItem:
    meta = ERROR_CODE_META[code]
    return ErrorItem(
        field=path if is_valid_field_path(path) else "listing",
        error_code=code,
        severity=severity,
        message=message,
        value_submitted=value,
        suggestion=suggestion,
        source=ErrorSource.AMAZON_ADAPTER,
        retryable=meta.retry_class in _RETRYABLE,
        marketplace=Marketplace.AMAZON,
        error_class=meta.error_class,
        retry_class=meta.retry_class,
    )


def sku_path(unit: AdapterItem) -> str:
    """Where an item-level problem is reported: `variants[<sku>]` for a child, else `listing`."""
    if unit.item_role == "child":
        path = f"variants[{unit.seller_sku}]"
        return path if is_valid_field_path(path) else "variants"
    return "listing"


def fact_path(unit: AdapterItem, key: str) -> str:
    """Where a seller-fact problem is reported: `facts.<key>`, or `variants[<sku>].facts.<key>`
    for a child (falling back to `facts.<key>` when the SKU makes no legal path)."""
    if unit.item_role == "child":
        path = f"variants[{unit.seller_sku}].facts.{key}"
        if is_valid_field_path(path):
            return path
    return f"facts.{key}"


# ---- values ----


def _entry(value: Any, marketplace_id: str, *, text: bool = True) -> dict[str, Any]:
    out: dict[str, Any] = {"value": value, "marketplace_id": marketplace_id}
    if text:
        out["language_tag"] = c.LANGUAGE_TAG
    return out


def _is_blank(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, list):
        return not any(not _is_blank(v) for v in value)
    return False


def map_enum(spec: AttributeSpec, value: str) -> str | None:
    """The Amazon spelling of an enum value, or None when it is not mappable."""
    key = normalize(value)
    for source, target in spec.value_map.items():
        if normalize(source) == key:
            return target
    if spec.allowed_values is None:
        return value if not spec.value_map else None
    for allowed in spec.allowed_values:
        if normalize(allowed) == key:
            return allowed
    return None


def _number(value: AttributeValue) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return value
    if isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
        return int(number) if number.is_integer() else number
    return None


def _attribute_entries(
    key: str, spec: AttributeSpec, value: AttributeValue, marketplace_id: str, path: str
) -> tuple[list[dict[str, Any]] | None, ErrorItem | None]:
    values = value if isinstance(value, list) else [value]
    values = [v for v in values if not _is_blank(v)]
    entries: list[dict[str, Any]] = []
    for one in values:
        if spec.value_type == "enum":
            mapped = map_enum(spec, str(one))
            if mapped is None:
                shown = str(one)
                return None, error(
                    path,
                    ErrorCode.VALUE_NOT_ALLOWED,
                    f"{shown} is not an allowed Amazon value for {key}.",
                    value=shown,
                    suggestion=_suggest(spec, shown),
                )
            entries.append(_entry(mapped, marketplace_id))
        elif spec.value_type in ("number", "measurement"):
            number = _number(one)
            if number is None:
                shown = str(one)
                return None, error(
                    path, ErrorCode.VALUE_NOT_ALLOWED, f"{key} must be a number.", value=shown
                )
            if spec.value_type == "measurement":
                entries.append(
                    {"value": number, "unit": spec.unit, "marketplace_id": marketplace_id}
                )
            else:
                entries.append(_entry(number, marketplace_id, text=False))
        elif spec.value_type == "boolean":
            if not isinstance(one, bool):
                return None, error(
                    path,
                    ErrorCode.VALUE_NOT_ALLOWED,
                    f"{key} must be true or false.",
                    value=str(one),
                )
            entries.append(_entry(one, marketplace_id, text=False))
        else:
            entries.append(_entry(str(one), marketplace_id))
    return entries, None


def _suggest(spec: AttributeSpec, value: str) -> str | None:
    if not spec.allowed_values:
        return None
    return "Allowed values include: " + ", ".join(nearest_values(value, spec.allowed_values))


# ---- SI2: the offer and the seller-fact attributes ----


def _money_error(unit: AdapterItem, key: str, amount: Decimal) -> ErrorItem | None:
    if amount <= 0 or amount > c.MAX_MONEY:
        return error(
            fact_path(unit, key),
            ErrorCode.VALUE_NOT_ALLOWED,
            f"{key} must be above 0 and at most {c.MAX_MONEY}.",
            value=str(amount),
        )
    return None


def _offer(
    unit: AdapterItem, facts: SellerFacts | None, marketplace_id: str
) -> tuple[dict[str, Any] | None, list[ErrorItem]]:
    """A sellable item's offer attributes, or None and the errors that keep it out."""
    errors: list[ErrorItem] = []
    mrp = None if facts is None else facts.mrp
    price = None if facts is None else facts.selling_price
    for key, amount in (("mrp", mrp), ("selling_price", price)):
        if amount is None:
            errors.append(
                error(
                    fact_path(unit, key),
                    ErrorCode.MISSING_ATTRIBUTE,
                    f"{key} is required to list the item for sale on Amazon.",
                )
            )
        elif (problem := _money_error(unit, key, amount)) is not None:
            errors.append(problem)
    currency = c.CURRENCY if facts is None or facts.currency is None else facts.currency
    if currency.strip().casefold() != c.CURRENCY.casefold():
        errors.append(
            error(
                fact_path(unit, "currency"),
                ErrorCode.VALUE_NOT_ALLOWED,
                f"currency must be {c.CURRENCY} on Amazon.in.",
                value=currency[:64],
            )
        )
    if not errors and mrp is not None and price is not None and price > mrp:
        errors.append(
            error(
                fact_path(unit, "selling_price"),
                ErrorCode.PRICE_ABOVE_MRP,
                "selling_price must not be above mrp.",
                value=str(price),
            )
        )
    if errors or mrp is None or price is None:
        return None, errors
    offer = {
        "marketplace_id": marketplace_id,
        "currency": c.CURRENCY,
        c.OUR_PRICE: [{"schedule": [{"value_with_tax": float(price)}]}],
        c.MRP_FIELD: [{"schedule": [{"value_with_tax": float(mrp)}]}],
    }
    return {
        c.OFFER_ATTRIBUTE: [offer],
        c.CONDITION_ATTRIBUTE: [{"value": c.CONDITION_NEW, "marketplace_id": marketplace_id}],
    }, []


def _identifier(
    unit: AdapterItem, facts: SellerFacts | None, marketplace_id: str, *, require: bool
) -> tuple[dict[str, Any], list[ErrorItem]]:
    """SI5 B2 (PLAN §2.7-2.8): a sellable item's barcode, else its GTIN exemption. With
    neither, a create (`require`) is IDENTIFIER_MISSING: the message names the SKU, never a
    value. An update of a live item is not refused (CP-D14). A parent sends neither."""
    if unit.item_role == "parent":
        return {}, []
    id_type = None if facts is None else c.IDENTIFIER_TYPES.get(facts.identifier_type or "")
    value = None if facts is None else facts.identifier_value
    if id_type is not None and value is not None and value.strip():
        entry = {"type": id_type, "value": value, "marketplace_id": marketplace_id}
        return {c.EXTERNAL_ID_ATTRIBUTE: [entry]}, []
    if facts is not None and facts.identifier_exempt:
        return {c.EXEMPTION_ATTRIBUTE: [{"value": True, "marketplace_id": marketplace_id}]}, []
    if not require:
        return {}, []
    message = (
        f"Amazon needs a barcode (EAN, UPC or GTIN) for {unit.seller_sku}, or mark it GTIN-exempt"
        " if Amazon approved an exemption for your brand."
    )
    return {}, [error(fact_path(unit, "identifier"), ErrorCode.IDENTIFIER_MISSING, message)]


def _fact_attributes(
    unit: AdapterItem,
    mapped: CategoryTemplate | None,
    facts: SellerFacts | None,
    marketplace_id: str,
) -> tuple[dict[str, list[dict[str, Any]]], list[str], list[ErrorItem]]:
    """(Amazon attribute -> entries, the `facts.*` paths sent, errors) for one item's seller
    facts. Each goes through the pinned template's spec of the same key; a missing manufacturer
    or country is MISSING_ATTRIBUTE whatever the template version (D6)."""
    out: dict[str, list[dict[str, Any]]] = {}
    sent: list[str] = []
    errors: list[ErrorItem] = []
    if mapped is None:
        return out, sent, errors
    for key in FACT_SPEC_KEYS:
        path = fact_path(unit, key)
        value: str | None
        if key == "country_of_origin":
            value = None if facts is None else facts.country_of_origin
        elif key == "manufacturer":
            value = None if facts is None else facts.manufacturer_name
        else:
            try:
                value = (
                    None
                    if facts is None
                    else composition_text(
                        facts.fiber_composition_text, facts.fiber_composition_parts
                    )
                )
            except ValueError:
                errors.append(
                    error(
                        path,
                        ErrorCode.VALUE_NOT_ALLOWED,
                        "fiber_composition must list each fibre with a percent, adding up to 100.",
                    )
                )
                continue
        if key in ("manufacturer", "country_of_origin") and _is_blank(value):
            errors.append(
                error(
                    path,
                    ErrorCode.MISSING_ATTRIBUTE,
                    f"{key} is required to list the item on Amazon.",
                )
            )
            continue
        if value is None or _is_blank(value) or _unsupported(mapped, key):
            continue
        spec = mapped.specs[key]
        shown = None if key == "manufacturer" else value
        entries, problem = _attribute_entries(key, spec, value, marketplace_id, path)
        if problem is not None:
            if shown is None:  # never echo a party value
                problem = problem.model_copy(update={"value_submitted": None, "suggestion": None})
            errors.append(problem)
            continue
        if not entries:
            continue
        out.setdefault(str(spec.marketplace_field), []).extend(entries)
        sent.append(f"facts.{key}")
        if key == "manufacturer" and facts is not None:
            address = (facts.manufacturer_address or "").strip()
            contact = f"{value}, {address}" if address else value
            out[c.MANUFACTURER_CONTACT] = [_entry(contact, marketplace_id)]
    return out, sent, errors


def _keywords(listing: CanonicalListing) -> str:
    """The Rules Engine's own joined keyword value, so what is sent is what was checked."""
    return submitted_keywords(listing.keywords)


def _unsupported(template: CategoryTemplate, key: str) -> bool:
    spec = template.specs.get(key)
    return (
        key in template.unsupported_attributes
        or spec is None
        or spec.requirement == "unsupported"
        or not spec.marketplace_field
    )


def required_keys(template: CategoryTemplate) -> list[str]:
    keys = set(template.required_attributes)
    keys |= {k for k, s in template.specs.items() if s.requirement == "required"}
    return sorted(keys)


def unit_attributes(listing: AdapterListing, unit: AdapterItem) -> dict[str, AttributeValue]:
    """The attribute values one item sends: the listing's, with a child's variant values on top;
    a parent leaves out the variation axes (its children carry them)."""
    attributes: dict[str, AttributeValue] = dict(listing.listing.attributes)
    if unit.item_role == "child":
        attributes.update(unit.variant_attributes)
    elif unit.item_role == "parent":
        for axis in _axes(listing) or frozenset():
            attributes.pop(axis, None)
    return attributes


def unit_paths(listing: AdapterListing, unit: AdapterItem) -> list[str]:
    paths = set(submitted_fields(listing.listing))
    paths |= {f"attributes.{k}" for k in unit.variant_attributes}
    return sorted(paths)


def item_payload(
    listing: AdapterListing,
    unit: AdapterItem,
    template: AdapterTemplate | None,
    *,
    marketplace_id: str = DEFAULT_MARKETPLACE_ID,
    preview: bool = False,
    require_identifier: bool = True,
) -> MappedItem:
    body = listing.listing
    attributes: dict[str, Any] = {}
    sent: list[str] = []
    skipped: list[str] = []
    errors: list[ErrorItem] = []

    attributes[c.CONTENT_FIELDS["content.title"]] = [_entry(body.content.title, marketplace_id)]
    bullets = [b for b in body.content.bullets if not _is_blank(b)]
    if bullets:
        attributes[c.CONTENT_FIELDS["content.bullets"]] = [
            _entry(b, marketplace_id) for b in bullets
        ]
        sent.append("content.bullets")
    else:
        skipped.append("content.bullets")
    attributes[c.CONTENT_FIELDS["content.description"]] = [
        _entry(body.content.description, marketplace_id)
    ]
    sent += ["content.title", "content.description"]
    keyword_paths = [p for p in c.KEYWORD_PATHS if getattr(body.keywords, p.split(".")[1])]
    if keyword_paths:
        attributes[c.KEYWORD_ATTRIBUTE] = [_entry(_keywords(body), marketplace_id)]
        sent += keyword_paths

    mapped = template.template if is_mapped(template) and template is not None else None
    values = unit_attributes(listing, unit)
    for key in sorted(set(body.attributes) | set(unit.variant_attributes)):
        path = f"attributes.{key}"
        if key not in values or mapped is None or _unsupported(mapped, key):
            skipped.append(path)
            continue
        spec = mapped.specs[key]
        where = path
        if unit.item_role == "child" and key in unit.variant_attributes:
            where = sku_path(unit)
        entries, problem = _attribute_entries(key, spec, values[key], marketplace_id, where)
        if problem is not None:
            errors.append(problem)
            skipped.append(path)
            continue
        if not entries:
            skipped.append(path)
            continue
        attributes.setdefault(str(spec.marketplace_field), []).extend(entries)
        sent.append(path)

    facts = listing.unit_facts.get(unit.product_id)
    extra, fact_sent, fact_errors = _fact_attributes(unit, mapped, facts, marketplace_id)
    for name, entries in extra.items():
        attributes.setdefault(name, []).extend(entries)
    sent += fact_sent
    errors += fact_errors
    if unit.item_role != "parent":
        offer, offer_errors = _offer(unit, facts, marketplace_id)
        errors += offer_errors
        if offer is not None:
            attributes.update(offer)
            sent += list(PRICE_PATHS)
    barcode, barcode_errors = _identifier(unit, facts, marketplace_id, require=require_identifier)
    attributes.update(barcode)
    errors += barcode_errors
    skipped += list(fact_paths(unit))

    if unit.item_role != "parent":
        for index, image in enumerate(body.images):
            path = f"images[{index}]"
            locator = listing.image_refs.get(image.image_id)
            if index >= c.MAX_IMAGES or not locator:
                skipped.append(path)
                continue
            if not locator_ok(locator, preview=preview, allow_http=listing.allow_http_images):
                errors.append(
                    error(
                        path,
                        ErrorCode.VALUE_NOT_ALLOWED,
                        "Each image must be a public https link.",
                    )
                )
                skipped.append(path)
                continue
            name = c.MAIN_IMAGE if index == 0 else c.OTHER_IMAGES[index - 1]
            attributes[name] = [{"media_location": locator, "marketplace_id": marketplace_id}]
            sent.append(path)
    else:
        skipped += [f"images[{i}]" for i in range(len(body.images))]

    axes = _axes(listing)
    if unit.item_role == "parent" and axes is not None and axes in c.AMAZON_THEMES:
        attributes[c.PARENTAGE_ATTRIBUTE] = [{"value": "parent", "marketplace_id": marketplace_id}]
        attributes[c.THEME_ATTRIBUTE] = [{"name": c.AMAZON_THEMES[axes]}]
    elif unit.item_role == "child":
        attributes[c.PARENTAGE_ATTRIBUTE] = [{"value": "child", "marketplace_id": marketplace_id}]
        attributes[c.RELATIONSHIP_ATTRIBUTE] = [
            {
                "child_relationship_type": "variation",
                "parent_sku": listing.seller_sku,
                "marketplace_id": marketplace_id,
            }
        ]
        if axes is not None and axes in c.AMAZON_THEMES:
            attributes[c.THEME_ATTRIBUTE] = [{"name": c.AMAZON_THEMES[axes]}]

    # SI11 (T10): the listing's adapter-side repairs, while their seller value is unchanged
    applied = apply_repairs(attributes, listing, unit)
    attributes = applied.attributes
    sent += [path for _id, path, _from, _to in applied.repaired]
    payload: Payload = {
        "productType": template.product_type if mapped is not None and template else None,
        "requirements": (
            c.REQUIREMENTS_PARENT if unit.item_role == "parent" else c.REQUIREMENTS_SELLABLE
        ),
        "attributes": {k: attributes[k] for k in sorted(attributes)},
    }
    return MappedItem(
        unit=unit,
        payload=payload,
        submitted=tuple(sorted(set(sent))),
        skipped=tuple(sorted(set(skipped) - set(sent))),
        errors=tuple(errors),
        paths=tuple(sorted(set(sent) | set(skipped))),
        repaired=applied.repaired,
        stale_repairs=applied.stale,
    )


def reverse_index(template: CategoryTemplate | None) -> Mapping[str, tuple[str, ...]]:
    """Amazon attribute -> the field paths that feed it (sorted)."""
    index: dict[str, set[str]] = {}
    for path, name in c.CONTENT_FIELDS.items():
        index.setdefault(name, set()).add(path)
    index[c.KEYWORD_ATTRIBUTE] = set(c.KEYWORD_PATHS)
    index[c.MAIN_IMAGE] = {"images[0]"}
    for i, name in enumerate(c.OTHER_IMAGES, start=1):
        index[name] = {f"images[{i}]"}
    for name in c.VARIATION_ATTRIBUTES:
        index[name] = {"variants"}
    # SI2: the offer feeds the two price paths; `condition_type` is left out, so an issue on it
    # fails `listing` and a price retry patches the offer only
    index[c.OFFER_ATTRIBUTE] = set(PRICE_PATHS)
    index[c.MANUFACTURER_CONTACT] = {"facts.manufacturer"}
    if template is not None:
        for key, spec in template.specs.items():
            if spec.marketplace_field:
                path = f"facts.{key}" if key in FACT_SPEC_KEYS else f"attributes.{key}"
                index.setdefault(spec.marketplace_field, set()).add(path)
    return {name: tuple(sorted(paths)) for name, paths in sorted(index.items())}


# ---- families and checks ----


def _axes(listing: AdapterListing) -> frozenset[str] | None:
    if listing.variation_theme is None:
        return None
    return c.NEUTRAL_THEME_AXES.get(listing.variation_theme)


def _family_errors(listing: AdapterListing, template: CategoryTemplate | None) -> list[ErrorItem]:
    units = listing.units()
    children = [u for u in units if u.item_role == "child"]
    is_family = listing.is_family
    if not is_family and listing.variation_theme is None:
        return []
    if not is_family:
        return [error("variants", ErrorCode.VARIATION_INVALID, "A variation theme needs a parent.")]
    axes = _axes(listing)
    if axes is None:
        return [
            error(
                "variants",
                ErrorCode.VARIATION_INVALID,
                "The variation theme is not one of size, color or size_color.",
                value=listing.variation_theme,
            )
        ]
    if template is not None and axes not in {frozenset(t) for t in template.variation_themes}:
        return [
            error(
                "variants",
                ErrorCode.VARIATION_INVALID,
                "The category mapping does not allow this variation theme on Amazon.",
                value=listing.variation_theme,
            )
        ]
    if not children:
        return [error("variants", ErrorCode.VARIATION_INVALID, "The family has no children.")]
    bad = [u.seller_sku for u in children if set(u.variant_attributes) != axes]
    if bad:
        return [
            error(
                "variants",
                ErrorCode.VARIATION_INVALID,
                "Every child must carry exactly the theme's variation attributes.",
                value=", ".join(bad),
            )
        ]
    return []


def build(
    listing: AdapterListing,
    *,
    marketplace_id: str = DEFAULT_MARKETPLACE_ID,
    schema: bool = True,
    preview: bool = False,
    require_identifier: bool = True,
) -> tuple[list[MappedItem], list[ErrorItem]]:
    """Every item's mapping and every blocking error (`check`), in item order. `schema=False`
    leaves out the stored-definition check: the update path (PATCH) is never blocked by it.
    `preview=True` (only from `preview()`) lets the opaque `image:<id>` tags through.
    `require_identifier=False` (the update path, CP-D14, and the preview of a live listing, whose
    next send is an update) does not refuse an item with no barcode and no exemption."""
    template = listing.template if is_mapped(listing.template) else None
    mapped = template.template if template is not None else None
    errors: list[ErrorItem] = []
    if template is None:
        errors.append(
            error(
                "listing",
                ErrorCode.CATEGORY_NOT_MAPPED,
                f"Category {listing.listing.category} has no Amazon mapping yet.",
            )
        )
    errors += _family_errors(listing, mapped)
    items = [
        item_payload(
            listing,
            unit,
            template,
            marketplace_id=marketplace_id,
            preview=preview,
            require_identifier=require_identifier,
        )
        for unit in listing.units()
    ]
    for unit in listing.units():
        if unit.seller_sku.strip() in UNSAFE_SKUS:
            errors.append(
                error(
                    sku_path(unit),
                    ErrorCode.VALUE_NOT_ALLOWED,
                    "A seller SKU may not be '.' or '..'.",
                    value=unit.seller_sku,
                )
            )
    seen: set[str] = set()
    if mapped is not None:
        for unit in listing.units():
            values = unit_attributes(listing, unit)
            axes = _axes(listing) or frozenset()
            for key in required_keys(mapped):
                if unit.item_role == "parent" and key in axes:
                    continue
                path = f"attributes.{key}"
                if path in seen or not _is_blank(values.get(key)):
                    continue
                seen.add(path)
                errors.append(
                    error(
                        path,
                        ErrorCode.MISSING_ATTRIBUTE,
                        f"{key} is required by the Amazon category mapping.",
                    )
                )
    seen_values: set[tuple[str, str | None]] = set()
    for item in items:
        for problem in item.errors:
            marker = (problem.field, problem.value_submitted)
            if marker not in seen_values:
                seen_values.add(marker)
                errors.append(problem)
    if schema:
        errors += _schema_errors(listing, items, template, seen)
    return items, errors


def _schema_errors(
    listing: AdapterListing,
    items: Sequence[MappedItem],
    template: AdapterTemplate | None,
    reported: set[str],
) -> list[ErrorItem]:
    """DW2: each item against the stored definition; schema errors have their own dedupe. SI2:
    a parent is checked against `parent_type_schema` (PRODUCT_ONLY), every other item against
    `product_type_schema` (LISTING); each is skipped when None (AC10)."""
    if template is None or not template.product_type:
        return []
    found: list[ErrorItem] = []
    for stored in (listing.product_type_schema, listing.parent_type_schema):
        if stored is not None and stored.product_type != template.product_type:
            return [
                error(
                    "listing",
                    ErrorCode.SCHEMA_INVALID,
                    f"The stored Amazon schema is for {stored.product_type}, not "
                    f"{template.product_type}; the listing was not checked against it.",
                    severity=Severity.WARNING,
                )
            ]
    index = reverse_index(template.template)
    markers: set[tuple[str, str, str | None, str]] = set()
    for item in items:
        schema = (
            listing.parent_type_schema
            if item.unit.item_role == "parent"
            else listing.product_type_schema
        )
        if schema is None:
            continue
        attributes = item.payload.get("attributes") or {}
        for problem in schema_check.check_item(attributes, schema, index, reported=reported):
            marker = (problem.field, problem.error_code.value, problem.raw_code, problem.message)
            if marker not in markers:
                markers.add(marker)
                found.append(problem)
    return found


def check(listing: AdapterListing) -> list[ErrorItem]:
    return build(listing)[1]


def family_payloads(
    listing: AdapterListing, *, marketplace_id: str = DEFAULT_MARKETPLACE_ID
) -> list[MappedItem]:
    """The parent first, then each child (a standalone listing is its one item)."""
    return build(listing, marketplace_id=marketplace_id)[0]


def report_for(errors: Sequence[ErrorItem], listing: AdapterListing) -> ErrorReport:
    blocking = [e for e in errors if e.severity is Severity.ERROR]
    return ErrorReport(
        status="failed" if blocking else "passed",
        error_code=blocking[0].error_code if blocking else None,
        errors=list(errors),
        listing_id=listing.listing_id,
    )


def preview(
    listing: AdapterListing, *, marketplace_id: str = DEFAULT_MARKETPLACE_ID
) -> AdapterPreview:
    items, errors = build(
        listing, marketplace_id=marketplace_id, preview=True, require_identifier=not listing.live
    )
    template = listing.template
    product_type = template.product_type if template is not None and is_mapped(template) else None
    return AdapterPreview(
        product_type=product_type,
        items=tuple(
            PreviewItem(
                product_id=i.unit.product_id,
                seller_sku=i.unit.seller_sku,
                item_role=i.unit.item_role,
                payload=i.payload,
            )
            for i in items
        ),
        report=report_for(errors, listing),
    )


# ---- S26 (DATABASE.md:757, :842): the product page of a live item, built here, never user input
# Amazon.in's detail page for an ASIN (believed: the public URL shape, not an SP-API answer; DW1).
DETAIL_PAGE_BASE: Final = "https://www.amazon.in/dp/"
MAX_LIVE_URL_CHARS: Final = 2048


def live_url(external_id: str | None) -> str | None:
    """The detail page URL for `external_id` (the ASIN), percent-quoted; None when it is blank
    or the URL would pass the 2048-character `live_url` limit."""
    from urllib.parse import quote

    if not isinstance(external_id, str) or not external_id.strip():
        return None
    url = DETAIL_PAGE_BASE + quote(external_id.strip(), safe="")
    return url if len(url) <= MAX_LIVE_URL_CHARS else None
