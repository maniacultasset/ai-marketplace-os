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


def _full_body(image_id: uuid.UUID) -> dict[str, Any]:
    """A version that stores the whole canonical listing (with stale ids the rows override)."""
    return {
        "listing_id": str(uuid.uuid4()),
        "product_id": str(uuid.uuid4()),
        "marketplace": "flipkart",
        "category": "home_decor_lamps",
        "version": 9,
        "content": CONTENT,
        "keywords": {"search_terms": ["kurta set"], "hidden_terms": []},
        "attributes": {"fabric_type": "cotton", "color": "navy blue"},
        "images": [{"image_id": str(image_id), "slot": "main", "position": 0}],
    }


def _listing(
    conn: Conn,
    tenant_id: uuid.UUID,
    product_id: uuid.UUID,
    *,
    content: dict[str, Any] | None = None,
    template_id: uuid.UUID | None = None,
    version: int = 1,
) -> uuid.UUID:
    listing_id = f.listing(conn, tenant_id, product_id, category=CATEGORY)
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
