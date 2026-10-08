"""Amazon adapter test helpers (S20): listings pinned to the seeded Amazon mappings."""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any

from app.adapters.base import AdapterItem, AdapterListing, AdapterTemplate
from mkos_schemas.listing import CanonicalListing
from tests.adapters.contract import family, item, sample_listing


def template(category: str = "ethnic_wear_kurta_sets") -> AdapterTemplate:
    from app.db.seed_templates import seed_templates

    (seed,) = [s for s in seed_templates() if s.template.category == category]
    return AdapterTemplate(seed.product_type, None, seed.template)


def listing(**attributes: Any) -> CanonicalListing:
    base = sample_listing()
    values = {**base.attributes, "fabric_type": "cotton", **attributes}
    return base.model_copy(
        update={"attributes": {k: v for k, v in values.items() if v is not None}}
    )


def image_refs(body: CanonicalListing) -> dict[uuid.UUID, str]:
    return {i.image_id: f"https://images.example.test/{i.image_id}.jpg" for i in body.images}


def amazon_item(
    body: CanonicalListing | None = None,
    *,
    sku: str | None = None,
    key: str | None = None,
    **over: Any,
) -> AdapterListing:
    body = body or listing()
    sent = item(body, sku=sku, key=key)
    fields: dict[str, Any] = {"template": template(), "image_refs": image_refs(body)}
    fields.update(over)
    return dataclasses.replace(sent, **fields)


def amazon_family(
    body: CanonicalListing | None = None,
    *,
    theme: str = "size_color",
    children: list[dict[str, str]] | None = None,
    **over: Any,
) -> AdapterListing:
    body = body or listing()
    sent = family(body)
    parent = sent.units()[0]
    rows = (
        children
        if children is not None
        else [
            {"size": "S", "color": "navy blue"},
            {"size": "S", "color": "maroon"},
            {"size": "M", "color": "navy blue"},
            {"size": "M", "color": "maroon"},
        ]
    )
    kids = tuple(
        AdapterItem(uuid.uuid4(), f"{parent.seller_sku}-{n}", "child", row)
        for n, row in enumerate(rows, start=1)
    )
    fields: dict[str, Any] = {
        "items": (parent, *kids),
        "variation_theme": theme,
        "template": template(),
        "image_refs": image_refs(body),
    }
    fields.update(over)
    return dataclasses.replace(sent, **fields)


def prepare(sent: AdapterListing) -> AdapterListing:
    """The contract suite's `prepare`: what S21's input builder adds for Amazon (the pinned
    kurta mapping and image locators), plus the `fabric_type` the mapping requires, which the
    shared fixture listing does not carry."""
    body = sent.listing
    if "fabric_type" not in body.attributes:
        body = body.model_copy(update={"attributes": {**body.attributes, "fabric_type": "cotton"}})
    return dataclasses.replace(sent, listing=body, template=template(), image_refs=image_refs(body))
