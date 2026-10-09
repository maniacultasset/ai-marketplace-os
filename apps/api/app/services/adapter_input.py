"""`build_adapter_listing`: the `AdapterListing` for a listing's current version (S20 T18; shared
with S21's publish job). Pure: it reads only the `PreviewSource` the repository returned.

- The canonical listing is the version's `content_json`, with the ids, marketplace, category and
  version number taken from the rows (never from the stored JSON). A version that stores only the
  `content` section (S14b's demo version) is read as that section, with the product's fact
  values as its attributes.
- A product with a `variation_theme` is a family: the parent is the listing's own SKU, then the
  live children in SKU order with their `variant_attributes`. Anything else is standalone.
  A marketplace whose families have no parent row (`registry.family_has_parent`: Flipkart, S22
  C9) gets the children only, so preview, publish and the item ledger see no parent item.
- Each child carries its colourway key and colour SKU, and `colour_image_refs` maps each colour
  key to the locators of that colourway's images (`image_refs` must hold them; S22 C1).
- The seller SKU is the product's `sku`; the template is the one the version pinned.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from mkos_schemas.listing import CanonicalListing

from app.adapters.base import AdapterItem, AdapterListing, AdapterTemplate
from app.adapters.registry import family_has_parent
from app.repositories.listings import PreviewSource

_SCALARS = (str, int, float, bool)


def _fact_values(attributes: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, fact in attributes.items():
        value = fact.get("value") if isinstance(fact, Mapping) else None
        if isinstance(value, _SCALARS) or (
            isinstance(value, list) and all(isinstance(v, str) for v in value)
        ):
            out[key] = value
    return out


def canonical_listing(source: PreviewSource) -> CanonicalListing:
    if source.version is None:
        raise ValueError("the listing has no current version")
    stored = dict(source.version.content)
    ids = {
        "listing_id": source.listing_id,
        "product_id": source.product.id,
        "marketplace": source.marketplace,
        "category": source.category,
        "version": source.version.version,
    }
    if "content" in stored:
        body = {**stored, **ids}
    else:
        body = {"content": stored, "attributes": _fact_values(source.product.attributes), **ids}
    return CanonicalListing.model_validate(body)


def _items(source: PreviewSource) -> tuple[AdapterItem, ...]:
    product = source.product
    if product.variation_theme is None:
        return (AdapterItem(product.id, product.sku, "standalone"),)
    children = tuple(
        AdapterItem(
            c.id,
            c.sku,
            "child",
            dict(c.variant_attributes),
            colour_key=c.colour_key,
            colour_sku=c.colour_sku,
        )
        for c in source.children
    )
    if not family_has_parent(source.marketplace):
        return children
    return (AdapterItem(product.id, product.sku, "parent"), *children)


def colour_image_ids(source: PreviewSource) -> set[uuid.UUID]:
    """Every colourway image id the listing's children may send (the caller makes their refs)."""
    return {image_id for ids in source.colour_images.values() for image_id in ids}


def build_adapter_listing(
    source: PreviewSource,
    *,
    image_refs: Mapping[uuid.UUID, str],
    idempotency_key: str,
    attempt: int = 1,
) -> AdapterListing:
    pinned = source.template
    template = (
        AdapterTemplate(pinned.product_type, pinned.external_category_id, pinned.template)
        if pinned is not None
        else None
    )
    return AdapterListing(
        listing_id=source.listing_id,
        seller_sku=source.product.sku,
        idempotency_key=idempotency_key,
        listing=canonical_listing(source),
        items=_items(source),
        variation_theme=source.product.variation_theme,
        template=template,
        attempt=attempt,
        image_refs=image_refs,
        colour_image_refs={
            key: refs
            for key, ids in source.colour_images.items()
            if (refs := tuple(image_refs[i] for i in ids if i in image_refs))
        },
    )
