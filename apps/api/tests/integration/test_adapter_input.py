"""S20 T18 (AC13, AC15): `ListingRepository.preview_source` (app_rw under RLS, through
`tenant_uow`) and the pure `build_adapter_listing` over it."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from . import factories as f
from .auth_support import World
from .conftest import LOCAL_HOSTS
from .family_support import family
from .seam_support import CATEGORY, Conn, add_template

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

CONTENT: dict[str, Any] = {
    "title": "Navy cotton kurta set",
    "bullets": ["Soft cotton"],
    "description": "A straight-fit cotton kurta set.",
}


def _full_body(image_id: uuid.UUID, *more: uuid.UUID) -> dict[str, Any]:
    """A version that stores the whole canonical listing (with stale ids the rows override)."""
    images = [
        {"image_id": str(i), "slot": "main" if n == 0 else "secondary", "position": n}
        for n, i in enumerate((image_id, *more))
    ]
    return {
        "listing_id": str(uuid.uuid4()),
        "product_id": str(uuid.uuid4()),
        "marketplace": "flipkart",
        "category": "home_decor_lamps",
        "version": 9,
        "content": CONTENT,
        "keywords": {"search_terms": ["kurta set"], "hidden_terms": []},
        "attributes": {"fabric_type": "cotton", "color": "navy blue"},
        "images": images,
    }


def _listing(
    conn: Conn,
    tenant_id: uuid.UUID,
    product_id: uuid.UUID,
    *,
    content: dict[str, Any] | None = None,
    template_id: uuid.UUID | None = None,
    version: int = 1,
    marketplace: str = "amazon",
) -> uuid.UUID:
    listing_id = f.listing(conn, tenant_id, product_id, category=CATEGORY, marketplace=marketplace)
    version_id = f.version(
        conn,
        tenant_id,
        listing_id,
        version=version,
        content_json=content if content is not None else CONTENT,
        category_template_id=template_id,
    )
    conn.execute(
        "UPDATE listings SET current_version_id = %s WHERE id = %s", [version_id, listing_id]
    )
    return listing_id


async def _source(world: World, listing_id: uuid.UUID, slug: str = "acme") -> Any:
    from app.repositories.uow import tenant_uow

    async with tenant_uow(world.tenant(slug)) as uow:
        return await uow.listings.preview_source(listing_id)


def _build(source: Any) -> Any:
    from app.services.adapter_input import build_adapter_listing

    return build_adapter_listing(source, image_refs={}, idempotency_key="preview")


async def test_adapter_input_standalone(products_world: World, auth_db: Conn) -> None:
    acme = products_world.tenant("acme")
    template_id = add_template(auth_db)
    product_id = f.product(auth_db, acme, category=CATEGORY, sku="TEE-STD-1")
    listing_id = _listing(auth_db, acme, product_id, template_id=template_id)
    source = await _source(products_world, listing_id)
    assert source.product.id == product_id and source.children == ()
    assert source.template is not None and source.template.id == template_id
    built = _build(source)
    assert built.seller_sku == "TEE-STD-1" and built.listing_id == listing_id
    assert [(i.product_id, i.item_role) for i in built.items] == [(product_id, "standalone")]
    assert built.variation_theme is None and not built.is_family
    assert built.template is not None and built.template.product_type == "KURTA"


async def test_adapter_input_family_parent_and_children(
    products_world: World, auth_db: Conn
) -> None:
    acme = products_world.tenant("acme")
    style, kids = family(auth_db, acme, "FAM-1", category=CATEGORY)
    listing_id = _listing(auth_db, acme, style, template_id=add_template(auth_db))
    built = _build(await _source(products_world, listing_id))
    assert built.is_family and built.variation_theme == "size_color"
    assert [(i.seller_sku, i.item_role) for i in built.items] == [
        ("FAM-1", "parent"),
        ("FAM-1-M-1", "child"),
        ("FAM-1-M-2", "child"),
        ("FAM-1-S-1", "child"),
        ("FAM-1-S-2", "child"),
    ]
    child = built.items[2]
    assert child.product_id == kids["FAM-1-M-2"]
    assert dict(child.variant_attributes) == {"size": "m", "color": "maroon"}


async def test_adapter_input_deleted_child_excluded(products_world: World, auth_db: Conn) -> None:
    acme = products_world.tenant("acme")
    style, kids = family(auth_db, acme, "DEL-1", sizes=("s",), category=CATEGORY)
    gone = kids["DEL-1-S-2"]
    with auth_db.transaction():
        auth_db.execute(
            "UPDATE variant_matrix_cells SET availability = 'unavailable', "
            "unavailable_reason = 'discontinued', variant_product_id = NULL "
            "WHERE variant_product_id = %s",
            [gone],
        )
        auth_db.execute("UPDATE products SET deleted_at = now() WHERE id = %s", [gone])
    listing_id = _listing(auth_db, acme, style)
    source = await _source(products_world, listing_id)
    assert [c.id for c in source.children] == [kids["DEL-1-S-1"]]


async def test_adapter_input_pinned_template_not_current(
    products_world: World, auth_db: Conn
) -> None:
    acme = products_world.tenant("acme")
    v1 = add_template(auth_db, version=1, is_current=False)
    v2 = add_template(
        auth_db, version=2, is_current=True, attributes={"required_attributes": ["x"]}
    )
    product_id = f.product(auth_db, acme, category=CATEGORY)
    listing_id = _listing(auth_db, acme, product_id, template_id=v1)
    source = await _source(products_world, listing_id)
    assert source.template.id == v1 != v2
    assert source.template.version == 1
    assert source.template.template.required_attributes == []


async def test_adapter_input_no_template_none(products_world: World, auth_db: Conn) -> None:
    acme = products_world.tenant("acme")
    product_id = f.product(auth_db, acme, category=CATEGORY)
    listing_id = _listing(auth_db, acme, product_id, template_id=None)
    source = await _source(products_world, listing_id)
    assert source.template is None and _build(source).template is None


async def test_adapter_input_no_version(products_world: World, auth_db: Conn) -> None:
    acme = products_world.tenant("acme")
    listing_id = f.listing(auth_db, acme, f.product(auth_db, acme), category=CATEGORY)
    source = await _source(products_world, listing_id)
    assert source is not None and source.version is None
    with pytest.raises(ValueError, match="no current version"):
        _build(source)


async def test_adapter_input_other_tenant_none(products_world: World, auth_db: Conn) -> None:
    acme = products_world.tenant("acme")
    listing_id = _listing(auth_db, acme, f.product(auth_db, acme, category=CATEGORY))
    assert await _source(products_world, listing_id, slug="bharat") is None
    assert await _source(products_world, uuid.uuid4()) is None
    assert await _source(products_world, listing_id) is not None


async def test_adapter_input_content_parsed_as_canonical(
    products_world: World, auth_db: Conn
) -> None:
    acme = products_world.tenant("acme")
    image_id = uuid.uuid4()
    product_id = f.product(auth_db, acme, category=CATEGORY)
    listing_id = _listing(auth_db, acme, product_id, content=_full_body(image_id), version=3)
    body = _build(await _source(products_world, listing_id)).listing
    assert (body.listing_id, body.product_id) == (listing_id, product_id)
    assert (body.marketplace.value, body.category, body.version) == ("amazon", CATEGORY, 3)
    assert body.content.title == CONTENT["title"]
    assert body.keywords.search_terms == ["kurta set"]
    assert body.attributes == {"fabric_type": "cotton", "color": "navy blue"}
    assert [i.image_id for i in body.images] == [image_id]


async def test_adapter_input_content_only_version_reads_product_facts(
    products_world: World, auth_db: Conn
) -> None:
    """S14b's demo version stores only the `content` section: the attributes come from the
    product's fact values."""
    acme = products_world.tenant("acme")
    product_id = f.product(
        auth_db,
        acme,
        category=CATEGORY,
        attributes={"fabric_type": f.fact(value="cotton"), "color": f.fact(value="indigo")},
    )
    listing_id = _listing(auth_db, acme, product_id)
    body = _build(await _source(products_world, listing_id)).listing
    assert body.content.title == CONTENT["title"]
    assert body.attributes == {"fabric_type": "cotton", "color": "indigo"}


# ---- S22 T7 (AC4; C1, C9; R7): Flipkart families, children only, with their colours ----


def _colourways(conn: Conn, style: uuid.UUID) -> dict[str, uuid.UUID]:
    rows = conn.execute(
        "SELECT colour_key, id FROM product_colourways WHERE style_product_id = %s", [style]
    ).fetchall()
    return {str(k): v for k, v in rows}


def _fk_listing(
    conn: Conn, tenant: uuid.UUID, style: uuid.UUID, content: dict[str, Any] | None = None
) -> uuid.UUID:
    template_id = add_template(conn, marketplace="flipkart")
    return _listing(
        conn, tenant, style, content=content, template_id=template_id, marketplace="flipkart"
    )


async def test_adapter_input_flipkart_family_children_only(
    products_world: World, auth_db: Conn
) -> None:
    acme = products_world.tenant("acme")
    style, _ = family(auth_db, acme, "FK-1", category=CATEGORY)
    built = _build(await _source(products_world, _fk_listing(auth_db, acme, style)))
    assert not built.is_family and built.is_parentless_family
    assert [(i.seller_sku, i.item_role) for i in built.items] == [
        ("FK-1-M-1", "child"),
        ("FK-1-M-2", "child"),
        ("FK-1-S-1", "child"),
        ("FK-1-S-2", "child"),
    ]
    assert built.seller_sku == "FK-1"


async def test_adapter_input_colour_key_and_sku_per_child(
    products_world: World, auth_db: Conn
) -> None:
    acme = products_world.tenant("acme")
    style, _ = family(auth_db, acme, "FK-2", category=CATEGORY)
    auth_db.execute(
        "UPDATE product_colourways SET colour_sku = 'FK-2-NVY' WHERE style_product_id = %s "
        "AND colour_key = 'navy blue'",
        [style],
    )
    built = _build(await _source(products_world, _fk_listing(auth_db, acme, style)))
    got = {i.seller_sku: (i.colour_key, i.colour_sku) for i in built.items}
    assert got == {
        "FK-2-M-1": ("navy blue", "FK-2-NVY"),
        "FK-2-M-2": ("maroon", None),
        "FK-2-S-1": ("navy blue", "FK-2-NVY"),
        "FK-2-S-2": ("maroon", None),
    }


async def test_adapter_input_colour_image_refs_grouped(
    products_world: World, auth_db: Conn
) -> None:
    from app.services.adapter_input import build_adapter_listing, colour_image_ids

    acme = products_world.tenant("acme")
    style, _ = family(auth_db, acme, "FK-3", category=CATEGORY)
    cw = _colourways(auth_db, style)
    second = f.image(auth_db, acme, style, colourway_id=cw["navy blue"], position=2)
    main = f.image(auth_db, acme, style, colourway_id=cw["navy blue"], slot="main", position=5)
    maroon = f.image(auth_db, acme, style, colourway_id=cw["maroon"], position=1)
    style_image = f.image(auth_db, acme, style)  # no colourway: not a colour image
    approved = _full_body(style_image, maroon, main, second)
    source = await _source(products_world, _fk_listing(auth_db, acme, style, approved))
    assert dict(source.colour_images) == {"maroon": (maroon,), "navy blue": (main, second)}
    ids = colour_image_ids(source)
    refs = {i: f"image:{i}" for i in ids}
    built = build_adapter_listing(source, image_refs=refs, idempotency_key="k")
    assert dict(built.colour_image_refs) == {
        "maroon": (f"image:{maroon}",),
        "navy blue": (f"image:{main}", f"image:{second}"),
    }


async def test_adapter_input_colour_images_only_from_approved_version(
    products_world: World, auth_db: Conn
) -> None:
    """Fix round 2 (F2): a colourway image the approved version does not hold (uploaded after
    approval, or never passed the image gate at approval) is never sent; a colourway left
    with none falls back to the listing's own images."""
    from app.adapters.flipkart.mapping import unit_images
    from app.services.adapter_input import build_adapter_listing, colour_image_ids

    acme = products_world.tenant("acme")
    style, _ = family(auth_db, acme, "FK-3B", category=CATEGORY)
    cw = _colourways(auth_db, style)
    style_image = f.image(auth_db, acme, style)
    navy = f.image(auth_db, acme, style, colourway_id=cw["navy blue"], slot="main")
    late = f.image(auth_db, acme, style, colourway_id=cw["navy blue"], position=3)
    unapproved = f.image(auth_db, acme, style, colourway_id=cw["maroon"], slot="main")
    auth_db.execute("UPDATE images SET compliance_status = 'failed' WHERE id = %s", [unapproved])
    approved = _full_body(style_image, navy)
    source = await _source(products_world, _fk_listing(auth_db, acme, style, approved))
    assert colour_image_ids(source) == {navy}
    refs = {i: f"image:{i}" for i in {style_image, navy, late, unapproved}}
    built = build_adapter_listing(source, image_refs=refs, idempotency_key="k")
    assert dict(built.colour_image_refs) == {"navy blue": (f"image:{navy}",)}
    sent = {i.colour_key: unit_images(built, i) for i in built.items}
    assert sent["navy blue"] == [f"image:{navy}"]
    assert sent["maroon"] == [f"image:{style_image}", f"image:{navy}"]


async def test_adapter_input_deleted_image_excluded(products_world: World, auth_db: Conn) -> None:
    acme = products_world.tenant("acme")
    style, _ = family(auth_db, acme, "FK-4", category=CATEGORY)
    cw = _colourways(auth_db, style)
    gone = f.image(auth_db, acme, style, colourway_id=cw["maroon"])
    auth_db.execute("UPDATE images SET deleted_at = now() WHERE id = %s", [gone])
    source = await _source(products_world, _fk_listing(auth_db, acme, style))
    assert dict(source.colour_images) == {}


async def test_adapter_input_size_only_theme_colour_none(
    products_world: World, auth_db: Conn
) -> None:
    """A size-only style's cells name no colourway (the 0004 matrix guard), so its children
    carry no colour and the adapter invents no colour group."""
    from .family_support import INSERT_CELL

    acme = products_world.tenant("acme")
    with auth_db.transaction():
        style = f.product(
            auth_db, acme, "style", sku="FK-5", variation_theme="size", size_run=["s", "m"],
            category=CATEGORY,
        )  # fmt: skip
        for size in ("s", "m"):
            child = f.product(
                auth_db, acme, "child", parent=style, sku=f"FK-5-{size.upper()}",
                category=CATEGORY, variant_attributes={"size": size},
                attributes={"size": f.fact(value=size.upper(), normalized=size)},
            )  # fmt: skip
            auth_db.execute(INSERT_CELL, (acme, style, None, size, child))
    built = _build(await _source(products_world, _fk_listing(auth_db, acme, style)))
    assert built.variation_theme == "size"
    assert {(i.colour_key, i.colour_sku) for i in built.items} == {(None, None)}


async def test_adapter_input_amazon_family_still_has_parent(
    products_world: World, auth_db: Conn
) -> None:
    acme = products_world.tenant("acme")
    style, _ = family(auth_db, acme, "AMZ-1", category=CATEGORY)
    listing_id = _listing(auth_db, acme, style, template_id=add_template(auth_db))
    built = _build(await _source(products_world, listing_id))
    assert built.items[0].item_role == "parent" and built.items[0].seller_sku == "AMZ-1"
    assert {i.colour_key for i in built.items[1:]} == {"navy blue", "maroon"}


async def test_adapter_input_other_tenant_colourway_invisible(
    products_world: World, auth_db: Conn
) -> None:
    """bharat's colourway images on acme's style id never reach acme's input, and bharat reads
    acme's listing as missing."""
    acme = products_world.tenant("acme")
    style, _ = family(auth_db, acme, "FK-6", category=CATEGORY)
    listing_id = _fk_listing(auth_db, acme, style)
    assert await _source(products_world, listing_id, slug="bharat") is None
    source = await _source(products_world, listing_id)
    assert {c.colour_key for c in source.children} == {"navy blue", "maroon"}
