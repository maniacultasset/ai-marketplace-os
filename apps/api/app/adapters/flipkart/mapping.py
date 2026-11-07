"""Canonical listing -> Flipkart listing payloads (spec L10573-10575, L10635; invariant 4; S22 T5,
T6; PLAN D3, D4; C6-C9).

Pure and deterministic: no HTTP, socket or database, and the same input always builds the same
payloads. The pinned category template's adapter-only columns drive it: `product_type` (the
Flipkart vertical), each spec's `marketplace_field` and `value_map` (DATABASE.md:541-542, T49).

- `item_payload(listing, unit, template)`: one item's `{sku, vertical, attributes}` body, the
  field paths it sends, the paths it skips (unsupported or unmapped, images past ten) and its
  value errors. Attribute values go as `[{"value": v}]`.
  - An enum value is looked up in the spec's `value_map` (normalized: NFKC, strip, casefold),
    else passed through in the template's spelling when it is an allowed value, else
    VALUE_NOT_ALLOWED with the submitted value (`navy blue` -> `Navy Blue`, L10635).
  - Both keyword lists go as one `keywords` value: a retry of either path sends the combined
    value and reports both (C7, as S20 C6).
- A family has **no parent row** (DATABASE.md:471, :754-755; T45): `family_payloads` sends the
  children only, each carrying `style_code` (the style's seller SKU) and `colour_group` (its
  `colour_sku`, else its `colour_key`), its variation values, and its colourway's images
  (`colour_image_refs`), else the listing's images (DATABASE.md:438).
- `check(listing)`: every error that blocks a create, so nothing is sent:
  - no mapped template -> CATEGORY_NOT_MAPPED (`listing`);
  - a required attribute missing or blank -> MISSING_ATTRIBUTE;
  - an unmappable enum -> VALUE_NOT_ALLOWED;
  - a bad family (a parent row, no children, a theme the template does not allow, a child
    without exactly the theme's axes) -> VARIATION_INVALID (`variants`);
  - a seller SKU holding `,` or `/`, or equal to `.` or `..` (C8) -> VALUE_NOT_ALLOWED.
- `preview(listing)`: what a create would send, per item, and the mapping report.
- `reverse_index(template)`: Flipkart attribute -> the field paths that feed it.

SI2: each item carries the seller's price block (`price`: MRP, selling price, INR) and the
seller facts `country_of_origin`, `fiber_composition` and `manufacturer` (`Manufacturer
Details`: name and address) through the template spec of the same key, at `facts.<key>`
(`variants[<sku>].facts.<key>` for a child). The error rules are Amazon's (SI2 T7-T8): missing
-> MISSING_ATTRIBUTE, price above MRP -> PRICE_ABOVE_MRP, another currency or an out-of-range
price -> VALUE_NOT_ALLOWED; an image locator must pass `locator_ok`; an enum VALUE_NOT_ALLOWED
suggests the nearest allowed values. No stock, HSN, package or fulfilment key is ever built.
The adapter only adds errors; it never removes or downgrades a Rules Engine verdict (invariant 2).
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from mkos_schemas.codes import ERROR_CODE_META, ErrorCode
from mkos_schemas.enums import ErrorSource, Marketplace, RetryClass, Severity
from mkos_schemas.errors import ErrorItem, ErrorReport
from mkos_schemas.jobs import AttributeSpec, CategoryTemplate
from mkos_schemas.listing import AttributeValue
from mkos_schemas.paths import is_valid_field_path

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
)
from app.adapters.flipkart import constants as c
from app.services.rules_engine.text import submitted_keywords

UNSAFE_SKUS: Final = frozenset({".", ".."})
UNSAFE_SKU_CHARS: Final = frozenset({",", "/"})
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
        source=ErrorSource.FLIPKART_ADAPTER,
        retryable=meta.retry_class in _RETRYABLE,
        marketplace=Marketplace.FLIPKART,
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


def unsafe_sku(sku: str) -> bool:
    """C8: a seller SKU the comma-joined GET path or a path segment could split or climb."""
    return sku.strip() in UNSAFE_SKUS or any(ch in sku for ch in UNSAFE_SKU_CHARS)


# ---- values ----


def _entry(value: Any) -> dict[str, Any]:
    return {"value": value}


def _is_blank(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, list):
        return not any(not _is_blank(v) for v in value)
    return False


def map_enum(spec: AttributeSpec, value: str) -> str | None:
    """The Flipkart spelling of an enum value, or None when it is not mappable."""
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
    key: str, spec: AttributeSpec, value: AttributeValue, path: str
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
                    f"{shown} is not an allowed Flipkart value for {key}.",
                    value=shown,
                    suggestion=_suggest(spec, shown),
                )
            entries.append(_entry(mapped))
        elif spec.value_type in ("number", "measurement"):
            number = _number(one)
            if number is None:
                return None, error(
                    path, ErrorCode.VALUE_NOT_ALLOWED, f"{key} must be a number.", value=str(one)
                )
            entry = _entry(number)
            if spec.value_type == "measurement" and spec.unit:
                entry["unit"] = spec.unit
            entries.append(entry)
        elif spec.value_type == "boolean":
            if not isinstance(one, bool):
                return None, error(
                    path,
                    ErrorCode.VALUE_NOT_ALLOWED,
                    f"{key} must be true or false.",
                    value=str(one),
                )
            entries.append(_entry(one))
        else:
            entries.append(_entry(str(one)))
    return entries, None


def _suggest(spec: AttributeSpec, value: str) -> str | None:
    if not spec.allowed_values:
        return None
    return "Allowed values include: " + ", ".join(nearest_values(value, spec.allowed_values))


# ---- SI2: the price block and the seller-fact attributes ----


def _price(
    unit: AdapterItem, facts: SellerFacts | None
) -> tuple[list[dict[str, Any]] | None, list[ErrorItem]]:
    errors: list[ErrorItem] = []
    mrp = None if facts is None else facts.mrp
    price = None if facts is None else facts.selling_price
    for key, amount in (("mrp", mrp), ("selling_price", price)):
        if amount is None:
            errors.append(
                error(
                    fact_path(unit, key),
                    ErrorCode.MISSING_ATTRIBUTE,
                    f"{key} is required to list the item for sale on Flipkart.",
                )
            )
        elif amount <= 0 or amount > c.MAX_MONEY:
            errors.append(
                error(
                    fact_path(unit, key),
                    ErrorCode.VALUE_NOT_ALLOWED,
                    f"{key} must be above 0 and at most {c.MAX_MONEY}.",
                    value=str(amount),
                )
            )
    currency = c.CURRENCY if facts is None or facts.currency is None else facts.currency
    if currency.strip().casefold() != c.CURRENCY.casefold():
        errors.append(
            error(
                fact_path(unit, "currency"),
                ErrorCode.VALUE_NOT_ALLOWED,
                f"currency must be {c.CURRENCY} on Flipkart.",
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
    block = {"mrp": float(mrp), "selling_price": float(price), "currency": c.CURRENCY}
    return [_entry(block)], []


def _fact_value(key: str, facts: SellerFacts | None) -> str | None:
    if facts is None:
        return None
    if key == "country_of_origin":
        return facts.country_of_origin
    if key == "manufacturer":
        name = (facts.manufacturer_name or "").strip()
        if not name:
            return None
        address = (facts.manufacturer_address or "").strip()
        return f"{name}, {address}" if address else name
    return composition_text(facts.fiber_composition_text, facts.fiber_composition_parts)


def _fact_attributes(
    unit: AdapterItem, mapped: CategoryTemplate | None, facts: SellerFacts | None
) -> tuple[dict[str, list[dict[str, Any]]], list[str], list[ErrorItem]]:
    """(Flipkart attribute -> entries, the `facts.*` paths sent, errors) for one item."""
    out: dict[str, list[dict[str, Any]]] = {}
    sent: list[str] = []
    errors: list[ErrorItem] = []
    if mapped is None:
        return out, sent, errors
    for key in FACT_SPEC_KEYS:
        path = fact_path(unit, key)
        try:
            value = _fact_value(key, facts)
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
                    f"{key} is required to list the item on Flipkart.",
                )
            )
            continue
        if value is None or _is_blank(value) or _unsupported(mapped, key):
            continue
        spec = mapped.specs[key]
        entries, problem = _attribute_entries(key, spec, value, path)
        if problem is not None:
            if key == "manufacturer":  # never echo a party value
                problem = problem.model_copy(update={"value_submitted": None, "suggestion": None})
            errors.append(problem)
            continue
        if entries:
            out.setdefault(str(spec.marketplace_field), []).extend(entries)
            sent.append(f"facts.{key}")
    return out, sent, errors


def keywords_value(listing: AdapterListing) -> str:
    """Both keyword lists as one value: deduplicated (normalized), first spelling kept. The
    Rules Engine's own joined value, so what is sent is what was checked."""
    return submitted_keywords(listing.listing.keywords)


def _unsupported(template: CategoryTemplate, key: str) -> bool:
    """Not sent: unsupported, unmapped, or a commercial field (price, MRP, stock, HSN, package,
    fulfilment) that v1 never sends, whatever a template maps it to."""
    spec = template.specs.get(key)
    return (
        key in template.unsupported_attributes
        or spec is None
        or spec.requirement == "unsupported"
        or not spec.marketplace_field
        or spec.marketplace_field.strip().casefold() in c.FORBIDDEN_ATTRIBUTES
    )


def required_keys(template: CategoryTemplate) -> list[str]:
    keys = set(template.required_attributes)
    keys |= {k for k, s in template.specs.items() if s.requirement == "required"}
    return sorted(keys)


def unit_attributes(listing: AdapterListing, unit: AdapterItem) -> dict[str, AttributeValue]:
    """The attribute values one item sends: the listing's, with a child's variant values on top."""
    attributes: dict[str, AttributeValue] = dict(listing.listing.attributes)
    if unit.item_role == "child":
        attributes.update(unit.variant_attributes)
    return attributes


def colour_group(unit: AdapterItem) -> str | None:
    """C7: the child's colour group: its colour SKU, else its colourway key, else its colour."""
    for value in (unit.colour_sku, unit.colour_key):
        if value is not None and value.strip():
            return value.strip()
    colour = unit.variant_attributes.get("color")
    return normalize(colour) if colour and colour.strip() else None


def unit_images(listing: AdapterListing, unit: AdapterItem) -> list[str | None]:
    """The image locators one item sends, in order: a child's colourway images when its
    colourway has any, else the listing's (None where an image has no locator)."""
    if unit.item_role == "child" and unit.colour_key is not None:
        refs = listing.colour_image_refs.get(unit.colour_key) or ()
        if refs:
            return list(refs)
    return [listing.image_refs.get(image.image_id) for image in listing.listing.images]


def item_payload(
    listing: AdapterListing,
    unit: AdapterItem,
    template: AdapterTemplate | None,
    *,
    preview: bool = False,
) -> MappedItem:
    body = listing.listing
    attributes: dict[str, list[dict[str, Any]]] = {}
    sent: list[str] = []
    skipped: list[str] = []
    errors: list[ErrorItem] = []

    attributes[c.CONTENT_FIELDS["content.title"]] = [_entry(body.content.title)]
    bullets = [b for b in body.content.bullets if not _is_blank(b)]
    if bullets:
        attributes[c.CONTENT_FIELDS["content.bullets"]] = [_entry(b) for b in bullets]
        sent.append("content.bullets")
    else:
        skipped.append("content.bullets")
    attributes[c.CONTENT_FIELDS["content.description"]] = [_entry(body.content.description)]
    sent += ["content.title", "content.description"]
    keyword_paths = [p for p in c.KEYWORD_PATHS if getattr(body.keywords, p.split(".")[1])]
    if keyword_paths:
        attributes[c.KEYWORD_ATTRIBUTE] = [_entry(keywords_value(listing))]
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
        entries, problem = _attribute_entries(key, spec, values[key], where)
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
    extra, fact_sent, fact_errors = _fact_attributes(unit, mapped, facts)
    for name, entries in extra.items():
        attributes.setdefault(name, []).extend(entries)
    sent += fact_sent
    errors += fact_errors
    if unit.item_role != "parent":
        block, price_errors = _price(unit, facts)
        errors += price_errors
        if block is not None:
            attributes[c.PRICE_ATTRIBUTE] = block
            sent += list(PRICE_PATHS)
    skipped += list(fact_paths(unit))

    images: list[dict[str, Any]] = []
    for index, locator in enumerate(unit_images(listing, unit)):
        path = f"images[{index}]"
        if index >= c.MAX_IMAGES or not locator:
            skipped.append(path)
            continue
        if not locator_ok(locator, preview=preview, allow_http=listing.allow_http_images):
            errors.append(
                error(path, ErrorCode.VALUE_NOT_ALLOWED, "Each image must be a public https link.")
            )
            skipped.append(path)
            continue
        images.append(_entry(locator))
        sent.append(path)
    if images:
        attributes[c.IMAGE_ATTRIBUTE] = images

    if unit.item_role == "child":
        attributes[c.STYLE_ATTRIBUTE] = [_entry(listing.seller_sku)]
        group = colour_group(unit)
        if group is not None:
            attributes[c.COLOUR_GROUP_ATTRIBUTE] = [_entry(group)]

    payload: Payload = {
        "sku": unit.seller_sku,
        "vertical": template.product_type if mapped is not None and template else None,
        "attributes": {k: attributes[k] for k in sorted(attributes)},
    }
    return MappedItem(
        unit=unit,
        payload=payload,
        submitted=tuple(sorted(set(sent))),
        skipped=tuple(sorted(set(skipped) - set(sent))),
        errors=tuple(errors),
        paths=tuple(sorted(set(sent) | set(skipped))),
    )


def reverse_index(template: CategoryTemplate | None) -> Mapping[str, tuple[str, ...]]:
    """Flipkart attribute -> the field paths that feed it (sorted). `images` feeds every image
    path; the group attributes feed `variants`."""
    index: dict[str, set[str]] = {}
    for path, name in c.CONTENT_FIELDS.items():
        index.setdefault(name, set()).add(path)
    index[c.KEYWORD_ATTRIBUTE] = set(c.KEYWORD_PATHS)
    index[c.IMAGE_ATTRIBUTE] = {f"images[{i}]" for i in range(c.MAX_IMAGES)}
    for name in c.GROUP_ATTRIBUTES:
        index[name] = {"variants"}
    index[c.PRICE_ATTRIBUTE] = set(PRICE_PATHS)  # SI2: the one price block
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


def _invalid(message: str, value: str | None = None) -> list[ErrorItem]:
    return [error("variants", ErrorCode.VARIATION_INVALID, message, value=value)]


def _family_errors(listing: AdapterListing, template: CategoryTemplate | None) -> list[ErrorItem]:
    if listing.is_family:
        return _invalid("A Flipkart family has no parent row; send the children only.")
    children = [u for u in listing.units() if u.item_role == "child"]
    if listing.variation_theme is None:
        return _invalid("Children need a variation theme.") if children else []
    axes = _axes(listing)
    if axes is None:
        return _invalid(
            "The variation theme is not one of size, color or size_color.",
            listing.variation_theme,
        )
    if template is not None and axes not in {frozenset(t) for t in template.variation_themes}:
        return _invalid(
            "The category mapping does not allow this variation theme on Flipkart.",
            listing.variation_theme,
        )
    if not children:
        return _invalid("The family has no children.")
    bad = [u.seller_sku for u in children if set(u.variant_attributes) != axes]
    if bad:
        return _invalid(
            "Every child must carry exactly the theme's variation attributes.", ", ".join(bad)
        )
    return []


def _sku_errors(listing: AdapterListing) -> list[ErrorItem]:
    errors: list[ErrorItem] = []
    if unsafe_sku(listing.seller_sku) and listing.is_parentless_family:
        errors.append(
            error(
                "listing",
                ErrorCode.VALUE_NOT_ALLOWED,
                "A seller SKU may not hold ',' or '/', or be '.' or '..'.",
                value=listing.seller_sku,
            )
        )
    for unit in listing.units():
        if unsafe_sku(unit.seller_sku):
            errors.append(
                error(
                    sku_path(unit),
                    ErrorCode.VALUE_NOT_ALLOWED,
                    "A seller SKU may not hold ',' or '/', or be '.' or '..'.",
                    value=unit.seller_sku,
                )
            )
    return errors


def build(
    listing: AdapterListing, *, preview: bool = False
) -> tuple[list[MappedItem], list[ErrorItem]]:
    """Every item's mapping and every blocking error (`check`), in item order. `preview=True`
    (only from `preview()`) lets the opaque `image:<id>` tags through."""
    template = listing.template if is_mapped(listing.template) else None
    mapped = template.template if template is not None else None
    errors: list[ErrorItem] = []
    if template is None:
        errors.append(
            error(
                "listing",
                ErrorCode.CATEGORY_NOT_MAPPED,
                f"Category {listing.listing.category} has no Flipkart mapping yet.",
            )
        )
    errors += _family_errors(listing, mapped)
    errors += _sku_errors(listing)
    items = [item_payload(listing, unit, template, preview=preview) for unit in listing.units()]
    if mapped is not None:
        seen: set[str] = set()
        for unit in listing.units():
            values = unit_attributes(listing, unit)
            for key in required_keys(mapped):
                path = f"attributes.{key}"
                if path in seen or not _is_blank(values.get(key)):
                    continue
                seen.add(path)
                errors.append(
                    error(
                        path,
                        ErrorCode.MISSING_ATTRIBUTE,
                        f"{key} is required by the Flipkart category mapping.",
                    )
                )
    seen_values: set[tuple[str, str | None]] = set()
    for item in items:
        for problem in item.errors:
            marker = (problem.field, problem.value_submitted)
            if marker not in seen_values:
                seen_values.add(marker)
                errors.append(problem)
    return items, errors


def check(listing: AdapterListing) -> list[ErrorItem]:
    return build(listing)[1]


def family_payloads(listing: AdapterListing) -> list[MappedItem]:
    """The children of a family (no parent row), or the one standalone item, in item order."""
    return build(listing)[0]


def report_for(errors: Sequence[ErrorItem], listing: AdapterListing) -> ErrorReport:
    blocking = [e for e in errors if e.severity is Severity.ERROR]
    return ErrorReport(
        status="failed" if blocking else "passed",
        error_code=blocking[0].error_code if blocking else None,
        errors=list(errors),
        listing_id=listing.listing_id,
    )


def preview(listing: AdapterListing) -> AdapterPreview:
    items, errors = build(listing, preview=True)
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
