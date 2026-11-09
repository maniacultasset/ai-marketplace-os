"""SI14 T25 (AC2; CANON.md:137): the column maps, the category suggestion and its review task,
the import's map, and the quality read, as `app_rw` on `mos_iso`: every read with another
tenant's ids is empty, every write changes nothing, and a service call names the foreign id as
missing."""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb
from tests.integration import factories as f
from tests.integration.auth_support import World, build_world

pytestmark = pytest.mark.usefixtures("iso_settings")
Conn = psycopg.Connection[tuple[object, ...]]
SUGGESTED: dict[str, Any] = {
    "status": "suggested",
    "candidates": [
        {"category": "tshirts", "confidence": 0.5, "rationale": "A tee.", "evidence_refs": []}
    ],
    "at": "2026-11-09T10:00:00Z",
}


@pytest.fixture
def world(clean_tenants: None, admin_conn: Conn) -> World:
    return build_world(admin_conn)


def _principal(world: World, email: str) -> Any:
    from app.core.rbac import Principal

    slug = email.split("@", 1)[1].split(".", 1)[0]
    return Principal(
        user_id=world.user(email),
        tenant_id=world.tenant(slug),
        role=email.split("@", 1)[0],  # type: ignore[arg-type]
        email=email,
        jti=str(uuid.uuid4()),
    )


def _their_map(conn: Conn, world: World) -> uuid.UUID:
    tenant = world.tenant("bharat")
    return f.insert(  # type: ignore[no-any-return]
        conn,
        "import_column_maps",
        tenant_id=tenant,
        status="ready",
        headers=["item sku", "product_name", "category"],
        headers_digest="0" * 64,
        mappings=Jsonb(
            [
                {
                    "position": 1,
                    "key": "seller_sku",
                    "method": "alias",
                    "confidence": 1.0,
                    "needs_review": False,
                },
                {
                    "position": 2,
                    "key": "product_name",
                    "method": "exact",
                    "confidence": 1.0,
                    "needs_review": False,
                },
                {
                    "position": 3,
                    "key": "category",
                    "method": "exact",
                    "confidence": 1.0,
                    "needs_review": False,
                },
            ]
        ),
        samples={"item sku": ["B-1"]},
        created_by_user_id=world.user("owner@bharat.test"),
    )


def _map_row(conn: Conn, map_id: uuid.UUID) -> tuple[Any, ...]:
    found = conn.execute(
        "SELECT status, mappings, samples, confirmed_at FROM import_column_maps WHERE id = %s",
        [map_id],
    ).fetchone()
    assert found is not None
    return tuple(found)


async def test_iso_column_map_reads_and_writes(world: World, admin_conn: Conn) -> None:
    from datetime import UTC, datetime

    from app.repositories.uow import tenant_uow

    theirs = _their_map(admin_conn, world)
    before = _map_row(admin_conn, theirs)
    async with tenant_uow(world.tenant("acme")) as uow:
        assert await uow.column_maps.get(theirs) is None
        assert await uow.column_maps.get_for_update(theirs) is None
        assert not await uow.column_maps.set_result(theirs, status="failed", mappings=[])
        assert not await uow.column_maps.confirm(theirs, mappings=[], at=datetime.now(UTC))
        await uow.column_maps.set_job(theirs, uuid.uuid4())
    assert _map_row(admin_conn, theirs) == before


async def test_iso_column_map_services_name_a_foreign_map_missing(
    world: World, admin_conn: Conn
) -> None:
    from app.core.errors import NotFoundError
    from app.services.imports import column_maps, upload

    theirs = _their_map(admin_conn, world)
    acme = _principal(world, "owner@acme.test")
    with pytest.raises(NotFoundError):
        await column_maps.get_map(acme, theirs)
    with pytest.raises(NotFoundError):
        await column_maps.confirm_map(
            acme, theirs, [{"position": 1, "key": None}], str(uuid.uuid4()), "/x"
        )
    assert await upload._confirmed_map(acme, theirs) is None


async def test_iso_category_suggestion_and_review(world: World, admin_conn: Conn) -> None:
    from app.repositories.uow import tenant_uow

    bharat = world.tenant("bharat")
    product = f.product(admin_conn, bharat, category="tshirts")
    admin_conn.execute(
        "UPDATE products SET category_suggestion = %s WHERE id = %s", [Jsonb(SUGGESTED), product]
    )
    task = f.human_task(
        admin_conn,
        bharat,
        product_id=product,
        kind="category_review",
        target_path=None,
        question="Which category fits this product?",
        reason="The suggested categories are not certain enough.",
        expected_answer={"kind": "enum", "allowed": ["tshirts"]},
    )
    async with tenant_uow(world.tenant("acme")) as uow:
        assert not await uow.products.write_category_suggestion(product, None)
        assert await uow.human_tasks.live_category_review(product) is None
    row = admin_conn.execute(
        "SELECT category_suggestion FROM products WHERE id = %s", [product]
    ).fetchone()
    assert row is not None and row[0] == SUGGESTED
    status = admin_conn.execute("SELECT status FROM human_tasks WHERE id = %s", [task]).fetchone()
    assert status == ("open",)


async def test_iso_category_suggest_service_names_a_foreign_product_missing(
    world: World, admin_conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import get_settings
    from app.core.errors import NotFoundError
    from app.services import category_suggest

    monkeypatch.setenv("CATEGORY_SUGGEST_ENABLED", "true")
    get_settings.cache_clear()
    product = f.product(admin_conn, world.tenant("bharat"), category="tshirts")
    with pytest.raises(NotFoundError):
        await category_suggest.request_suggestion(
            _principal(world, "owner@acme.test"), product, str(uuid.uuid4()), "/x"
        )
    jobs = admin_conn.execute(
        "SELECT count(*) FROM job_runs WHERE entity_type = 'category_suggestion'"
    ).fetchone()
    assert jobs == (0,)


async def test_iso_quality_reads_only_the_callers_tenant(world: World, admin_conn: Conn) -> None:
    from datetime import UTC, datetime, timedelta

    from app.repositories.quality import QualityRepository
    from app.repositories.transactions import unit_of_work

    bharat = world.tenant("bharat")
    listing = f.listing(admin_conn, bharat)
    f.version(admin_conn, bharat, listing)
    acme = world.tenant("acme")
    now = datetime.now(UTC)
    async with unit_of_work(acme) as session:
        repo = QualityRepository(session, acme)
        read = await repo.quality(now - timedelta(days=1), now + timedelta(days=1))
    assert read.rows == {} and read.edited_fields == []
