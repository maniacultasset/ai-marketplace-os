"""S26 T20 (AC 9; spec L2108, "normalizes 'Jet Black' ... while preserving the seller value"): a
family child's colour axis goes to the adapter as its colourway's normalized colour when the
seller gave one (CSV `normalized_color`), not the display key; the seller value stays on the
colourway. Found by the acceptance run: FSH-TEE-001 "Jet Black" (normalized `black`) was refused
by the Amazon mapping (`jet black` is not an allowed value) before any call."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from . import factories as f
from .auth_support import World
from .conftest import LOCAL_HOSTS
from .family_support import INSERT_CELL
from .seam_support import CATEGORY, Conn, add_template

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

CONTENT: dict[str, Any] = {
    "title": "Women's relaxed fit cotton T-shirt",
    "bullets": ["Soft cotton"],
    "description": "A relaxed-fit cotton T-shirt.",
}


def _family(conn: Conn, tenant: uuid.UUID, normalized: str | None) -> uuid.UUID:
    colour = (
        {"value": "Jet Black"}
        if normalized is None
        else {"value": "Jet Black", "normalized": normalized}
    )
    with conn.transaction():
        style = f.product(
            conn, tenant, "style", sku="FSH-NC-1", size_run=["s", "m"], category=CATEGORY
        )
        colourway = f.colourway(conn, tenant, style, attributes={"color": f.fact(**colour)})
        for size in ("s", "m"):
            child = f.product(
                conn,
                tenant,
                "child",
                parent=style,
                sku=f"FSH-NC-1-{size.upper()}",
                category=CATEGORY,
                variant_attributes={"size": size, "color": "jet black"},
                attributes={"size": f.fact(value=size.upper(), normalized=size)},
            )
            conn.execute(INSERT_CELL, (tenant, style, colourway, size, child))
    listing_id = f.listing(conn, tenant, style, category=CATEGORY, marketplace="amazon")
    version_id = f.version(
        conn,
        tenant,
        listing_id,
        version=1,
        content_json=CONTENT,
        category_template_id=add_template(conn),
    )
    conn.execute(
        "UPDATE listings SET current_version_id = %s WHERE id = %s", [version_id, listing_id]
    )
    return listing_id


async def _children(world: World, listing_id: uuid.UUID) -> list[Any]:
    from app.repositories.uow import tenant_uow
    from app.services.adapter_input import build_adapter_listing

    async with tenant_uow(world.tenant("acme")) as uow:
        source = await uow.listings.preview_source(listing_id)
    assert source is not None
    built = build_adapter_listing(source, image_refs={}, idempotency_key="preview")
    return [i for i in built.items if i.item_role == "child"]


async def test_family_child_colour_is_the_normalized_colour(
    products_world: World, auth_db: Conn
) -> None:
    listing_id = _family(auth_db, products_world.tenant("acme"), "black")
    children = await _children(products_world, listing_id)
    assert [c.seller_sku for c in children] == ["FSH-NC-1-M", "FSH-NC-1-S"]
    for child in children:
        assert dict(child.variant_attributes) == {
            "size": child.seller_sku[-1].lower(),
            "color": "black",
        }
        assert child.colour_key == "jet black"  # the colourway identity is unchanged
    stored = auth_db.execute(
        "SELECT attributes -> 'color' ->> 'value' FROM product_colourways WHERE style_product_id = "
        "(SELECT product_id FROM listings WHERE id = %s)",
        [listing_id],
    ).fetchone()
    assert stored == ("Jet Black",)  # the seller value is preserved


async def test_family_child_colour_without_normalized_keeps_the_key(
    products_world: World, auth_db: Conn
) -> None:
    listing_id = _family(auth_db, products_world.tenant("acme"), None)
    children = await _children(products_world, listing_id)
    assert {c.variant_attributes["color"] for c in children} == {"jet black"}
