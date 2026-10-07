"""S14b T5 (AC 3, AC 7, AC 8; C4, C6, CD4): the repository reads the seam and the rules API use,
as `app_rw` under RLS, through `tenant_uow`."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text

from .auth_support import World
from .conftest import LOCAL_HOSTS
from .ops_support import attribute_definition, category_attribute
from .products_support import statements
from .seam_support import (
    CATEGORY,
    Conn,
    add_image,
    add_product,
    add_rule,
    add_template,
    banned,
    max_length,
    node_of,
    revision_of,
    running_job,
    terminal_job,
)

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)


def _uow(world: World, slug: str = "acme") -> Any:
    from app.repositories.uow import tenant_uow

    return tenant_uow(world.tenant(slug), user_id=world.user(f"owner@{slug}.test"))


# ---- rules ----


async def test_list_for_resolution_includes_disabled_and_revision(
    products_world: World, auth_db: Conn
) -> None:
    on = add_rule(auth_db, max_length(200))
    off = add_rule(auth_db, max_length(150), enabled=False, version=2)
    async with _uow(products_world) as uow:
        records = await uow.rules.list_for_resolution("amazon", CATEGORY)
    by_id = {r.rule.id: r for r in records}
    assert set(by_id) == {on, off}
    assert by_id[off].rule.enabled is False
    assert by_id[on].revision == revision_of(auth_db, on)
    assert by_id[off].revision == revision_of(auth_db, off) > by_id[on].revision
    assert by_id[on].tenant_id is None and by_id[on].subject == ""


async def test_list_for_resolution_platform_plus_own_not_other(
    products_world: World, auth_db: Conn
) -> None:
    platform = add_rule(auth_db, max_length())
    own = add_rule(auth_db, banned(["cheap"]), tenant_id=products_world.tenant("acme"))
    add_rule(auth_db, banned(["costly"]), tenant_id=products_world.tenant("bharat"))
    async with _uow(products_world) as uow:
        records = await uow.rules.list_for_resolution("amazon", CATEGORY)
    assert {r.rule.id for r in records} == {platform, own}
    assert {r.tenant_id for r in records} == {None, products_world.tenant("acme")}


async def test_list_for_resolution_null_category_rows(products_world: World, auth_db: Conn) -> None:
    wide = add_rule(auth_db, max_length(300), category=None)
    add_rule(auth_db, max_length(100), category="home_decor_lamps")
    add_rule(auth_db, max_length(100), marketplace="flipkart")
    async with _uow(products_world) as uow:
        records = await uow.rules.list_for_resolution("amazon", CATEGORY)
    assert [r.rule.id for r in records] == [wide]
    assert records[0].rule.category is None


async def test_list_page_keyset_stable(products_world: World, auth_db: Conn) -> None:
    made = [add_rule(auth_db, max_length(100 + n), version=n + 1) for n in range(5)]
    seen: list[uuid.UUID] = []
    after = None
    pages = 0
    while True:
        async with _uow(products_world) as uow:
            rows, more = await uow.rules.list_page(limit=2, after=after)
        seen += [r.id for r in rows]
        pages += 1
        if not more:
            break
        after = (rows[-1].created_at, rows[-1].id)
    assert pages == 3
    assert sorted(seen) == sorted(made) and len(seen) == len(set(seen))
    async with _uow(products_world) as uow:
        everything, more = await uow.rules.list_page(limit=200, after=None)
    assert [r.id for r in everything] == seen and more is False


async def test_list_page_filters(products_world: World, auth_db: Conn) -> None:
    kurta = add_rule(auth_db, max_length())
    lamps = add_rule(auth_db, max_length(), category="home_decor_lamps")
    flip = add_rule(auth_db, max_length(), marketplace="flipkart")
    async with _uow(products_world) as uow:
        amazon, _ = await uow.rules.list_page(limit=50, after=None, marketplace="amazon")
        lamp, _ = await uow.rules.list_page(limit=50, after=None, category="home_decor_lamps")
        both, _ = await uow.rules.list_page(
            limit=50, after=None, marketplace="flipkart", category=CATEGORY
        )
    assert {r.id for r in amazon} == {kurta, lamps}
    assert [r.id for r in lamp] == [lamps]
    assert [r.id for r in both] == [flip]


async def test_list_page_other_tenant_rows_absent(products_world: World, auth_db: Conn) -> None:
    mine = add_rule(auth_db, banned(["cheap"]), tenant_id=products_world.tenant("acme"))
    theirs = add_rule(auth_db, banned(["costly"]), tenant_id=products_world.tenant("bharat"))
    async with _uow(products_world, "bharat") as uow:
        rows, _ = await uow.rules.list_page(limit=50, after=None)
    assert [r.id for r in rows] == [theirs]
    assert mine not in {r.id for r in rows}


@pytest.mark.parametrize("limit", [0, 201, True])
async def test_list_page_limit_refused(products_world: World, limit: Any) -> None:
    async with _uow(products_world) as uow:
        with pytest.raises(ValueError, match="limit"):
            await uow.rules.list_page(limit=limit, after=None)


# ---- job_runs.node ----


async def test_set_node_running_only(products_world: World, auth_db: Conn) -> None:
    tenant = products_world.tenant("acme")
    running = running_job(auth_db, tenant)
    queued = running_job(
        auth_db, tenant, status="queued", attempt=0, started_at=None, lease_expires_at=None
    )
    done = terminal_job(auth_db, tenant)
    async with _uow(products_world) as uow:
        assert await uow.job_runs.set_node(running, "retrieve_brand") is True
        assert await uow.job_runs.set_node(queued, "retrieve_brand") is False
        assert await uow.job_runs.set_node(done, "retrieve_brand") is False
    assert node_of(auth_db, running) == "retrieve_brand"
    assert node_of(auth_db, queued) is None
    assert node_of(auth_db, done) is None


async def test_set_node_other_tenant_false(products_world: World, auth_db: Conn) -> None:
    theirs = running_job(auth_db, products_world.tenant("bharat"))
    async with _uow(products_world) as uow:
        assert await uow.job_runs.set_node(theirs, "content_agent") is False
    assert node_of(auth_db, theirs) is None


# ---- images ----


async def test_reports_for_product_marketplace_key_only(
    products_world: World, auth_db: Conn
) -> None:
    tenant = products_world.tenant("acme")
    product = add_product(auth_db, tenant)
    both = add_image(auth_db, tenant, product, "amazon", "flipkart")
    flipkart_only = add_image(auth_db, tenant, product, "flipkart")
    async with _uow(products_world) as uow:
        amazon = await uow.images.reports_for_product(product, "amazon")
        flipkart = await uow.images.reports_for_product(product, "flipkart")
    assert set(amazon) == {both}
    assert amazon[both].marketplace.value == "amazon"
    assert set(flipkart) == {both, flipkart_only}


async def test_reports_for_product_other_tenant_absent(
    products_world: World, auth_db: Conn
) -> None:
    bharat = products_world.tenant("bharat")
    product = add_product(auth_db, bharat)
    add_image(auth_db, bharat, product, "amazon")
    async with _uow(products_world) as uow:
        assert await uow.images.reports_for_product(product, "amazon") == {}


async def test_reports_for_product_unchecked_absent(products_world: World, auth_db: Conn) -> None:
    tenant = products_world.tenant("acme")
    product = add_product(auth_db, tenant)
    add_image(auth_db, tenant, product)
    gone = add_image(auth_db, tenant, product, "amazon")
    auth_db.execute("UPDATE images SET deleted_at = now() WHERE id = %s", [gone])
    async with _uow(products_world) as uow:
        assert await uow.images.reports_for_product(product, "amazon") == {}


# ---- templates ----


async def test_template_current_only(products_world: World, auth_db: Conn) -> None:
    add_template(auth_db, version=1, is_current=False)
    current = add_template(
        auth_db, version=2, attributes={"required_attributes": ["color"], "specs": {}}
    )
    async with _uow(products_world) as uow:
        found = await uow.templates.current("amazon", CATEGORY)
        absent = await uow.templates.current("flipkart", CATEGORY)
    assert found is not None and found.id == current and found.version == 2
    assert found.template.required_attributes == ["color"]
    assert found.template.category == CATEGORY
    assert absent is None


async def test_uow_new_repos_share_session(products_world: World) -> None:
    async with _uow(products_world) as uow:
        sessions = {
            id(uow.session),
            id(uow.rules.db),
            id(uow.brand_chunks.db),
            id(uow.job_runs.db),
            id(uow.images.db),
            id(uow.templates.db),
            id(uow.dictionary.db),
        }
        tenants = {uow.rules.tenant_id, uow.job_runs.tenant_id, uow.images.tenant_id}
    assert len(sessions) == 1
    assert tenants == {products_world.tenant("acme")}


# ---- dictionary revisions (C6) ----


def _max_definition_revision(conn: Conn) -> int:
    row = conn.execute("SELECT coalesce(max(revision), 0) FROM attribute_definitions").fetchone()
    assert row is not None
    return int(row[0])


async def test_dictionary_revisions_include_disabled(products_world: World, auth_db: Conn) -> None:
    # attribute_definitions is platform-global and outlives the per-test reset: fresh keys only
    attribute_definition(auth_db, key=f"s14b_{uuid.uuid4().hex[:12]}", enabled=False)
    top = _max_definition_revision(auth_db)
    async with _uow(products_world) as uow:
        definitions, _ = await uow.dictionary.revisions(CATEGORY)
    assert definitions == top


async def test_dictionary_revisions_category_scoped(products_world: World, auth_db: Conn) -> None:
    key = attribute_definition(auth_db, key=f"s14b_{uuid.uuid4().hex[:12]}")
    category_attribute(auth_db, key, category=CATEGORY, enabled=False)
    mine = auth_db.execute(
        "SELECT max(revision) FROM category_attributes WHERE category = %s", [CATEGORY]
    ).fetchone()
    category_attribute(auth_db, key, category="home_decor_lamps")
    async with _uow(products_world) as uow:
        _, category_max = await uow.dictionary.revisions(CATEGORY)
    assert mine is not None and category_max == int(mine[0])


async def test_dictionary_revisions_empty_zero(products_world: World) -> None:
    async with _uow(products_world) as uow:
        _, category_max = await uow.dictionary.revisions("no_such_category")
    assert category_max == 0


async def test_dictionary_revisions_one_statement(products_world: World) -> None:
    async with _uow(products_world) as uow:
        await uow.session.execute(text("SELECT 1"))  # the GUC statements run first
        with statements() as seen:
            await uow.dictionary.revisions(CATEGORY)
    assert len(seen) == 1
