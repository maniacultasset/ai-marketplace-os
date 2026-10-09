"""S18 T2 (AC 8; D5, D6; DATABASE.md:231, :1189): the resolved facts read and the brand profile the
prompt sees, as `app_rw` against rows the admin login wrote."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from . import factories as f
from .conftest import LOCAL_HOSTS
from .products_support import statements

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)


@pytest.fixture
def world(products_world: Any) -> Any:
    return products_world


async def _resolve(tenant: uuid.UUID, product_id: uuid.UUID) -> Any:
    from app.repositories.uow import tenant_uow

    async with tenant_uow(tenant) as uow:
        return await uow.product_facts.resolve(product_id)


async def _brand(tenant: uuid.UUID) -> dict[str, Any]:
    from app.repositories.uow import tenant_uow

    async with tenant_uow(tenant) as uow:
        return await uow.brand_profile.for_prompt()


async def test_resolve_standalone(world: Any) -> None:
    acme = world.tenant("acme")
    maker = f.party(world.conn, acme, gstin="29ABCDE1234F1Z5", phone="+91-80-1")
    p = f.product(
        world.conn,
        acme,
        sku="KURTA-9",
        title="Cotton Kurta",
        mrp="1499.00",
        manufacturer_party_id=maker,
        attributes={"color": f.fact(value="indigo")},
    )
    facts = await _resolve(acme, p)
    assert facts is not None
    assert (facts.sku, facts.level, facts.identity.title) == (
        "KURTA-9",
        "standalone",
        "Cotton Kurta",
    )
    assert facts.commercial.mrp == "1499.00"
    assert facts.compliance.manufacturer.gstin == "29ABCDE1234F1Z5"
    assert facts.attributes["color"].value == "indigo"
    assert facts.attributes["color"].inherited_from is None


async def test_resolve_style_with_available_cells_only(world: Any) -> None:
    acme = world.tenant("acme")
    style = f.product(world.conn, acme, "style", variation_theme="size", size_run=["s", "m", "l"])
    with world.conn.transaction():  # the deferred matrix check wants the child's cell too
        child = f.product(
            world.conn,
            acme,
            "child",
            parent=style,
            variant_attributes={"size": "m"},
            attributes={"size": f.fact(value="M", normalized="m")},
        )
        f.cell(
            world.conn,
            acme,
            style,
            size_key="m",
            availability="available",
            unavailable_reason=None,
            variant_product_id=child,
        )
    f.cell(world.conn, acme, style, size_key="l")  # unavailable
    facts = await _resolve(acme, style)
    assert facts is not None and facts.level == "style" and facts.family is not None
    assert [c.size_key for c in facts.family.cells] == ["m"]
    assert facts.family.size_run == ["s", "m", "l"]


async def test_resolve_brand_default_origin(world: Any) -> None:
    acme = world.tenant("acme")
    maker = f.party(world.conn, acme, legal_name="Default Maker Pvt Ltd")
    f.brand_profile(
        world.conn,
        acme,
        default_country_of_origin="IN",
        default_manufacturer_party_id=maker,
        provenance={"country_of_origin": f.fact(value="IN"), "manufacturer": f.fact(value=None)},
    )
    p = f.product(world.conn, acme)
    facts = await _resolve(acme, p)
    assert facts is not None
    assert facts.compliance.country_of_origin == "IN"
    assert facts.compliance.manufacturer.legal_name == "Default Maker Pvt Ltd"
    assert facts.provenance["country_of_origin"].inherited_from == "brand"


async def test_resolve_soft_deleted_none(world: Any) -> None:
    acme = world.tenant("acme")
    p = f.product(world.conn, acme, deleted_at="2026-10-01T00:00:00Z")
    assert await _resolve(acme, p) is None


async def test_resolve_other_tenant_none(world: Any) -> None:
    p = f.product(world.conn, world.tenant("bharat"))
    assert await _resolve(world.tenant("acme"), p) is None


async def test_resolve_four_statements(world: Any) -> None:
    acme = world.tenant("acme")
    maker = f.party(world.conn, acme)
    p = f.product(world.conn, acme, manufacturer_party_id=maker)
    from app.repositories.uow import tenant_uow

    async with tenant_uow(acme) as uow:
        with statements() as seen:
            assert await uow.product_facts.resolve(p) is not None
    reads = [s for s in seen if s.lstrip().upper().startswith("SELECT") and "set_config" not in s]
    assert len(reads) <= 4, reads


async def test_resolve_weight_provenance_dictionary_version(world: Any) -> None:
    acme = world.tenant("acme")
    p = f.product(world.conn, acme, product_weight_g=450)
    facts = await _resolve(acme, p)
    row = world.conn.execute("SELECT max(revision) FROM attribute_definitions").fetchone()
    assert facts is not None
    assert facts.product_weight_g == 450
    assert "product_weight" in facts.provenance
    assert facts.dictionary_version == int(row[0])


# ---- the brand profile the prompt sees (D6) ----


async def test_brand_profile_for_prompt_fields_exact(world: Any) -> None:
    acme = world.tenant("acme")
    world.conn.execute(
        "UPDATE tenants SET tone = 'warm', guidelines = '[\"Lead with fabric.\"]'::jsonb, "
        'glossary = \'{"kurta": "tunic"}\'::jsonb WHERE id = %s',
        [acme],
    )
    f.brand_profile(
        world.conn,
        acme,
        brand_names=["Acme"],
        brand_owner_type="brand_owner",
        brand_description="Handloom cotton.",
        website_url="https://acme.example",
    )
    shown = await _brand(acme)
    assert set(shown) == {
        "tone",
        "guidelines",
        "glossary",
        "brand_names",
        "brand_owner_type",
        "content_locale",
        "brand_description",
    }
    assert shown["tone"] == "warm" and shown["brand_names"] == ["Acme"]
    assert "acme.example" not in repr(shown)


async def test_brand_profile_for_prompt_no_brand_profiles_row(world: Any) -> None:
    shown = await _brand(world.tenant("acme"))
    assert shown["content_locale"] == "en-IN"
    assert shown["brand_names"] == [] and shown["brand_description"] is None


async def test_brand_profile_for_prompt_description_capped_2000(world: Any) -> None:
    acme = world.tenant("acme")
    f.brand_profile(world.conn, acme, brand_description="x" * 2000)
    assert len((await _brand(acme))["brand_description"]) == 2000


async def test_brand_profile_for_prompt_other_tenant_absent(world: Any) -> None:
    f.brand_profile(world.conn, world.tenant("bharat"), brand_description="Bharat only.")
    shown = await _brand(world.tenant("acme"))
    assert shown["brand_description"] is None
