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
  - a seller SKU `.` or `..` (C16) -> VALUE_NOT_ALLOWED.
- `family_payloads(listing)`: the parent first (content, theme, `parentage_level parent`), then
  each child (content, attributes with its variant values, images, the parent link).
- `preview(listing)`: what a create would send, per item, and the mapping report.
- `reverse_index(template)`: Amazon attribute -> the field paths that feed it, so a marketplace
  issue on `generic_keyword` fails both keyword paths.

The adapter only adds errors; it never removes or downgrades a Rules Engine verdict (invariant
2): `required_attributes` and the enum vocabulary are the ones the engine checks.
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
from mkos_schemas.listing import AttributeValue, CanonicalListing
from mkos_schemas.paths import is_valid_field_path

from app.adapters.amazon import constants as c
from app.adapters.base import (
    AdapterItem,
    AdapterListing,
    AdapterPreview,
    AdapterTemplate,
    PreviewItem,
    submitted_fields,
)

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
) -> ErrorItem:
    meta = ERROR_CODE_META[code]
    return ErrorItem(
        field=path if is_valid_field_path(path) else "listing",
        error_code=code,
        severity=severity,
        message=message,
        value_submitted=value,
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


def _keywords(listing: CanonicalListing) -> str:
    seen: set[str] = set()
    terms: list[str] = []
    for term in [*listing.keywords.search_terms, *listing.keywords.hidden_terms]:
        key = normalize(term)
        if key and key not in seen:
            seen.add(key)
            terms.append(term.strip())
    return " ".join(terms)


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

    if unit.item_role != "parent":
        for index, image in enumerate(body.images):
            path = f"images[{index}]"
            locator = listing.image_refs.get(image.image_id)
            if index >= c.MAX_IMAGES or not locator:
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

    payload: Payload = {
        "productType": template.product_type if mapped is not None and template else None,
        "requirements": c.REQUIREMENTS,
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
    if template is not None:
        for key, spec in template.specs.items():
            if spec.marketplace_field:
                index.setdefault(spec.marketplace_field, set()).add(f"attributes.{key}")
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
    listing: AdapterListing, *, marketplace_id: str = DEFAULT_MARKETPLACE_ID
) -> tuple[list[MappedItem], list[ErrorItem]]:
    """Every item's mapping and every blocking error (`check`), in item order."""
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
        item_payload(listing, unit, template, marketplace_id=marketplace_id)
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
    if mapped is not None:
        seen: set[str] = set()
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
    return items, errors


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
    items, errors = build(listing, marketplace_id=marketplace_id)
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
