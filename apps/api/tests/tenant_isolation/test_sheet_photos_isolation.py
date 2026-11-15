"""G2 (CANON.md:137): the sheet photo link repository as `app_rw` on `mos_iso`. Every read with
another tenant's ids is empty, every write changes nothing, a download job never claims another
tenant's links, and a write through another tenant's unit of work is refused."""

from __future__ import annotations

import uuid

import psycopg
import pytest
from tests.integration import factories as f
from tests.integration.auth_support import World, build_world

pytestmark = pytest.mark.usefixtures("iso_settings")
Conn = psycopg.Connection[tuple[object, ...]]


@pytest.fixture
def world(clean_tenants: None, admin_conn: Conn) -> World:
    return build_world(admin_conn)


def _links(conn: Conn, tenant: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    product = f.product(conn, tenant, category="kurtas")
    failed = f.photo_link(conn, tenant, product, status="failed", problem="timeout")
    queued = f.photo_link(conn, tenant, product, position=2)
    return product, failed, queued


def _statuses(conn: Conn, product: uuid.UUID) -> list[str]:
    rows = conn.execute(
        "SELECT status FROM photo_links WHERE product_id = %s ORDER BY position", [product]
    ).fetchall()
    return [str(r[0]) for r in rows]


async def test_iso_photo_link_reads_and_writes(world: World, admin_conn: Conn) -> None:
    from app.repositories.uow import tenant_uow

    acme, bharat = world.tenant("acme"), world.tenant("bharat")
    product, failed, queued = _links(admin_conn, bharat)
    job = uuid.uuid4()
    async with tenant_uow(acme) as uow:
        repo = uow.photo_links
        assert await repo.get_link(failed) is None
        assert await repo.for_product(product) == []
        assert not await repo.waiting_for_product(product)
        assert await repo.products_with_queued() == []
        assert await repo.claim(product, job) == []  # a job never claims another tenant's links
        assert await repo.requeue_failed(product_id=product) == []
        assert not await repo.dismiss(failed)
        assert not await repo.mark_failed(queued, job, "timeout")
        assert await repo.cancel_for_products([product]) == []
        assert await repo.release(job) == []
    assert _statuses(admin_conn, product) == ["failed", "queued"]


async def test_iso_import_reads_see_only_own_links(world: World, admin_conn: Conn) -> None:
    from app.repositories.uow import tenant_uow

    acme, bharat = world.tenant("acme"), world.tenant("bharat")
    import_id = uuid.uuid4()
    product = f.product(admin_conn, bharat, category="kurtas")
    f.photo_link(admin_conn, bharat, product, import_id=import_id, row_number=2)
    async with tenant_uow(acme) as uow:
        assert await uow.photo_links.for_import_rows(import_id, [2]) == {2: []}
        assert await uow.photo_links.totals_for_import(import_id) is None
        assert await uow.photo_links.requeue_failed(import_id=import_id) == []


async def test_iso_write_through_another_tenants_unit_is_refused(
    world: World, admin_conn: Conn
) -> None:
    from app.db.guard import CrossTenantQueryError
    from app.repositories.photo_links import PhotoLinkRepository
    from app.repositories.uow import tenant_uow

    acme, bharat = world.tenant("acme"), world.tenant("bharat")
    product, failed, _queued = _links(admin_conn, bharat)
    async with tenant_uow(acme) as uow:
        foreign = PhotoLinkRepository(uow.session, bharat)
        with pytest.raises(CrossTenantQueryError):
            await foreign.dismiss(failed)
    assert _statuses(admin_conn, product) == ["failed", "queued"]
