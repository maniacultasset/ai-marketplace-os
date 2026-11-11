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
    p = f.product(world.conn, acme, manufacturer_party_id=maker, brand_name="Acme")
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


# ---- SI1 T9 (AC7, seam S-b): structured party fields and the override order ----


async def test_resolve_structured_party_fields(world: Any) -> None:
    acme = world.tenant("acme")
    maker = f.party(
        world.conn,
        acme,
        address_line1="12 MG Road",
        address_line2="Floor 2",
        city="Bengaluru",
        state_code="KA",
        postal_code="560001",
    )
    p = f.product(world.conn, acme, manufacturer_party_id=maker)
    facts = await _resolve(acme, p)
    assert facts is not None
    m = facts.compliance.manufacturer
    assert (m.address_line1, m.address_line2, m.city, m.state_code, m.postal_code) == (
        "12 MG Road",
        "Floor 2",
        "Bengaluru",
        "KA",
        "560001",
    )
    assert m.country_code == "IN"
    assert m.address == "12 MG Road, Floor 2, Bengaluru, KA, 560001, IN"


async def test_product_override_beats_brand_default(world: Any) -> None:
    from app.repositories.product_facts import manufacturer_inherited

    acme = world.tenant("acme")
    default = f.party(world.conn, acme, legal_name="Brand Default Pvt Ltd")
    own = f.party(world.conn, acme, legal_name="Own Maker Pvt Ltd")
    f.brand_profile(
        world.conn,
        acme,
        default_manufacturer_party_id=default,
        provenance={"manufacturer": f.fact(value=None)},
    )
    overridden = f.product(world.conn, acme, manufacturer_party_id=own)
    plain = f.product(world.conn, acme)
    facts = await _resolve(acme, overridden)
    assert facts is not None and facts.compliance.manufacturer.legal_name == "Own Maker Pvt Ltd"
    inherited = await _resolve(acme, plain)
    assert inherited is not None
    assert inherited.compliance.manufacturer.legal_name == "Brand Default Pvt Ltd"

    class _Row:
        def __init__(self, party: uuid.UUID | None) -> None:
            self.manufacturer_party_id = party

    assert manufacturer_inherited(_Row(None), None)  # type: ignore[arg-type]
    assert not manufacturer_inherited(_Row(own), None)  # type: ignore[arg-type]
    assert not manufacturer_inherited(_Row(None), _Row(own))  # type: ignore[arg-type]


async def test_style_override_reaches_its_variant(world: Any) -> None:
    acme = world.tenant("acme")
    style_maker = f.party(world.conn, acme, legal_name="Style Maker Pvt Ltd")
    style = f.product(
        world.conn,
        acme,
        "style",
        variation_theme="size",
        size_run=["m"],
        manufacturer_party_id=style_maker,
    )
    with world.conn.transaction():
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
    facts = await _resolve(acme, child)
    assert facts is not None
    assert facts.compliance.manufacturer.legal_name == "Style Maker Pvt Ltd"


# ---- SI2 T4 (AC2, AC5, AC14): a family's facts ----


async def _family(tenant: uuid.UUID, product_id: uuid.UUID) -> Any:
    from app.repositories.uow import tenant_uow

    async with tenant_uow(tenant) as uow:
        return await uow.product_facts.resolve_family(product_id)


async def test_resolve_family_children_in_sku_order_skip_deleted(world: Any) -> None:
    from .family_support import family

    acme = world.tenant("acme")
    style, children = family(world.conn, acme, f"SI2-{uuid.uuid4().hex[:6]}", sizes=("s", "m"))
    gone = sorted(children)[0]
    with world.conn.transaction():  # the matrix check wants the cell retired with the child
        world.conn.execute(
            "UPDATE variant_matrix_cells SET availability = 'unavailable',"
            " unavailable_reason = 'not_produced', variant_product_id = NULL"
            " WHERE variant_product_id = %s",
            [children[gone]],
        )
        world.conn.execute("UPDATE products SET deleted_at = now() WHERE id = %s", [children[gone]])
    found = await _family(acme, style)
    assert found is not None
    main, kids = found
    assert main.level == "style"
    assert [k.sku for k in kids] == sorted(s for s in children if s != gone)
    # AC2: a child with no own price inherits the style's
    assert {k.commercial.selling_price for k in kids} == {"499.00"}
    assert {k.commercial.mrp for k in kids} == {"999.00"}


async def test_resolve_family_standalone_has_no_children(world: Any) -> None:
    acme = world.tenant("acme")
    p = f.product(world.conn, acme, mrp="10.00")
    found = await _family(acme, p)
    assert found is not None and found[1] == ()
    assert await _family(acme, uuid.uuid4()) is None


async def test_resolve_family_brand_default_manufacturer_address(world: Any) -> None:
    acme = world.tenant("acme")
    f.brand_manufacturer(world.conn, acme)
    p = f.product(world.conn, acme)
    found = await _family(acme, p)
    assert found is not None
    maker = found[0].compliance.manufacturer
    assert maker is not None and maker.legal_name == "Test Mills Pvt Ltd"
    assert maker.address is not None and "1 Mill Road" in maker.address
    # a product override party wins
    own = f.party(world.conn, acme, legal_name="Own Maker Pvt Ltd")
    q = f.product(world.conn, acme, manufacturer_party_id=own)
    over = await _family(acme, q)
    assert over is not None and over[0].compliance.manufacturer.legal_name == "Own Maker Pvt Ltd"


async def test_resolve_family_other_tenant_children_never_returned(world: Any) -> None:
    """AC14. A product naming another tenant's party cannot exist (the composite FK
    `fk_products_manufacturer_party_id` on (tenant_id, party_id)), so the family read is the
    isolation surface left: tenant B's style and children never resolve under tenant A."""
    from .family_support import family

    acme, bharat = world.tenant("acme"), world.tenant("bharat")
    style, children = family(world.conn, bharat, f"SI2B-{uuid.uuid4().hex[:6]}")
    assert await _family(acme, style) is None
    for child in children.values():
        assert await _family(acme, child) is None
    mine = await _family(bharat, style)
    assert mine is not None and len(mine[1]) == len(children)


async def test_critical_keys_from_the_dictionary(world: Any) -> None:
    """SI4 A6 (CP-D9): the `is_critical` dictionary keys (DATABASE.md:1229)."""
    from app.repositories.uow import tenant_uow

    async with tenant_uow(world.tenant("acme")) as uow:
        keys = await uow.product_facts.critical_keys()
    assert {"fiber_composition", "country_of_origin"} <= keys
    assert "color" not in keys


# ---- SI8 T2 (AC1; CP-D1): the brand default ----


def _size_child(world: Any, tenant: uuid.UUID, style: uuid.UUID) -> uuid.UUID:
    child = f.product(
        world.conn,
        tenant,
        "child",
        parent=style,
        variant_attributes={"size": "m"},
        attributes={"size": f.fact(value="M", normalized="m")},
    )
    f.cell(
        world.conn,
        tenant,
        style,
        size_key="m",
        availability="available",
        unavailable_reason=None,
        variant_product_id=child,
    )
    return child


async def test_brand_default_skips_blank_profile_names(world: Any) -> None:
    acme = world.tenant("acme")
    f.brand_profile(world.conn, acme, brand_names=["", "Kuinn"])
    facts = await _resolve(acme, f.product(world.conn, acme))
    assert facts is not None and facts.identity.brand_name == "Kuinn"


async def test_brand_style_override_wins_and_reaches_its_variant(world: Any) -> None:
    acme = world.tenant("acme")
    f.brand_profile(world.conn, acme, brand_names=["Kuinn"])
    style = f.product(
        world.conn, acme, "style", variation_theme="size", size_run=["m"], brand_name="Acme Own"
    )
    with world.conn.transaction():
        child = _size_child(world, acme, style)
    own = await _resolve(acme, style)
    inherited = await _resolve(acme, child)
    assert own is not None and own.identity.brand_name == "Acme Own"
    assert inherited is not None and inherited.identity.brand_name == "Acme Own"


async def test_brand_default_empty_profile_uses_the_tenant_name(world: Any) -> None:
    acme = world.tenant("acme")
    world.conn.execute("UPDATE tenants SET name = '  Acme Store ' WHERE id = %s", [acme])
    f.brand_profile(world.conn, acme, brand_names=[])
    facts = await _resolve(acme, f.product(world.conn, acme))
    assert facts is not None and facts.identity.brand_name == "Acme Store"
    no_row = world.tenant("bharat")
    world.conn.execute("UPDATE tenants SET name = 'Bharat Store' WHERE id = %s", [no_row])
    facts = await _resolve(no_row, f.product(world.conn, no_row))
    assert facts is not None and facts.identity.brand_name == "Bharat Store"


async def test_brand_default_family_reads_once(world: Any) -> None:
    acme = world.tenant("acme")
    f.brand_profile(world.conn, acme, brand_names=["Kuinn"])
    style = f.product(world.conn, acme, "style", variation_theme="size", size_run=["m"])
    with world.conn.transaction():
        _size_child(world, acme, style)
    from app.repositories.uow import tenant_uow

    async with tenant_uow(acme) as uow:
        found = await uow.product_facts.resolve_family(style)
    assert found is not None
    main, children = found
    assert [main.identity.brand_name, *(c.identity.brand_name for c in children)] == ["Kuinn"] * 2


async def test_brand_default_never_reads_another_tenant(world: Any) -> None:
    """The acting user is a member of both tenants, so RLS's `tenants_switcher` policy lets the app
    role see both tenant rows: only the repository's tenant scope keeps B's name and brand names
    out of A's resolve."""
    acme, bharat = world.tenant("acme"), world.tenant("bharat")
    both = world.user("owner@acme.test")
    world.conn.execute(
        "INSERT INTO tenant_memberships (tenant_id, user_id, role) VALUES (%s, %s, 'owner')",
        [bharat, both],
    )
    world.conn.execute("UPDATE tenants SET name = 'Acme Store' WHERE id = %s", [acme])
    world.conn.execute("UPDATE tenants SET name = 'Bharat Store' WHERE id = %s", [bharat])
    f.brand_profile(world.conn, bharat, brand_names=["Bharat Brand"])
    p = f.product(world.conn, acme)
    from app.repositories.uow import tenant_uow

    async with tenant_uow(acme, user_id=both) as uow:
        facts = await uow.product_facts.resolve(p)
        choices = await uow.brand_profile.brand_choices()
    assert facts is not None and facts.identity.brand_name == "Acme Store"
    assert choices == ("Acme Store",)


async def _set_website(tenant: uuid.UUID, url: str | None) -> bool:
    from app.repositories.uow import tenant_uow

    async with tenant_uow(tenant) as uow:
        return await uow.brand_profiles.set_website(url)


async def test_brand_website_never_moves_a_product_digest(world: Any) -> None:
    """Client item 1: the brand provenance is a fact layer of every product, so setting or
    clearing the brand website must leave every product's facts digest (the approval pin)
    as it was."""
    from app.core.resolved_facts import facts_digest

    acme = world.tenant("acme")
    f.brand_profile(world.conn, acme, brand_names=["Acme"])
    p = f.product(world.conn, acme, sku="KURTA-W", mrp="999.00")
    before = await _resolve(acme, p)
    assert before is not None
    assert await _set_website(acme, "https://acme.example") is True
    stored = await _resolve(acme, p)
    assert stored is not None and "website_url" not in stored.provenance
    assert facts_digest(stored) == facts_digest(before)
    assert await _set_website(acme, None) is True
    cleared = await _resolve(acme, p)
    assert cleared is not None and facts_digest(cleared) == facts_digest(before)
