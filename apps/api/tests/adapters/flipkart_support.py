"""Flipkart adapter test helpers (S22): listings pinned to the seeded Flipkart mappings, and the
client's ports over the in-process fake marketplace (no network)."""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any

from app.adapters.base import AdapterItem, AdapterListing, AdapterTemplate
from mkos_schemas.listing import CanonicalListing
from tests.adapters.amazon_support import CountingLimiter, FakeTokens, FixedRng
from tests.adapters.contract import item, sample_listing

__all__ = ["CountingLimiter", "FakeTokens", "FixedRng"]
FK_API = "http://127.0.0.1:4020/flipkart/sellers"
SELLER = "FKSELLER01"


def template(category: str = "ethnic_wear_kurta_sets") -> AdapterTemplate:
    from app.db.seed_templates import flipkart_seed_templates

    (seed,) = [s for s in flipkart_seed_templates() if s.template.category == category]
    return AdapterTemplate(seed.product_type, None, seed.template)


def listing(**attributes: Any) -> CanonicalListing:
    base = sample_listing()
    values = {**base.attributes, "fabric_type": "cotton", **attributes}
    return base.model_copy(
        update={"attributes": {k: v for k, v in values.items() if v is not None}}
    )


def image_refs(body: CanonicalListing) -> dict[uuid.UUID, str]:
    return {i.image_id: f"https://images.example.test/{i.image_id}.jpg" for i in body.images}


def fk_item(
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


DEFAULT_CHILDREN = (
    ("S", "navy blue"),
    ("S", "maroon"),
    ("M", "navy blue"),
    ("M", "maroon"),
)


def fk_family(
    body: CanonicalListing | None = None,
    *,
    theme: str = "size_color",
    children: list[dict[str, str]] | None = None,
    colour_sku: dict[str, str] | None = None,
    style: str | None = None,
    **over: Any,
) -> AdapterListing:
    """A parentless family: the children only (S22 C9), each with its colourway key."""
    body = body or listing()
    style_sku = style or f"FKFAM-{uuid.uuid4().hex[:8]}"
    rows = (
        children
        if children is not None
        else [{"size": s, "color": col} for s, col in DEFAULT_CHILDREN]
    )
    skus = colour_sku or {}
    kids = []
    for row in rows:
        colour = row.get("color")
        key = colour.strip().lower() if colour else None
        suffix = "-".join(v.replace(" ", "").upper() for v in row.values())
        kids.append(
            AdapterItem(
                uuid.uuid4(),
                f"{style_sku}-{suffix}",
                "child",
                row,
                colour_key=key,
                colour_sku=skus.get(key or ""),
            )
        )
    kids.sort(key=lambda k: k.seller_sku)
    fields: dict[str, Any] = {
        "items": tuple(kids),
        "variation_theme": theme,
        "template": template(),
        "image_refs": image_refs(body),
    }
    fields.update(over)
    return AdapterListing(
        listing_id=body.listing_id or uuid.uuid4(),
        seller_sku=style_sku,
        idempotency_key=uuid.uuid4().hex,
        listing=body,
        **fields,
    )


def prepare(sent: AdapterListing) -> AdapterListing:
    """The contract suite's `prepare`: what the shared input builder produces for Flipkart (no
    parent row, `family_has_parent("flipkart")` is false; each child's colourway key), plus the
    pinned kurta mapping, image locators and the `fabric_type` the mapping requires."""
    body = sent.listing
    if "fabric_type" not in body.attributes:
        body = body.model_copy(update={"attributes": {**body.attributes, "fabric_type": "cotton"}})
    items = tuple(
        dataclasses.replace(
            u,
            colour_key=(u.variant_attributes.get("color") or "").strip().lower() or None,
        )
        for u in sent.items
        if u.item_role != "parent"
    )
    return dataclasses.replace(
        sent, listing=body, items=items, template=template(), image_refs=image_refs(body)
    )
