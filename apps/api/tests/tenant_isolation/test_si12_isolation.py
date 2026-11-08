"""SI12 T22 (AC13; CANON.md:137): the photo role, the size chart and its read as `app_rw` on
`mos_iso`: every read with another tenant's ids is empty, every write changes nothing, and a
write through another tenant's unit of work is refused."""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pytest
from tests.integration import factories as f
from tests.integration.auth_support import World, build_world

pytestmark = pytest.mark.usefixtures("iso_settings")
Conn = psycopg.Connection[tuple[object, ...]]
HASH = "c" * 64
CHART = {"unit": "cm", "basis": "garment", "rows": [{"size": "M", "chest": 100}]}


@pytest.fixture
def world(clean_tenants: None, admin_conn: Conn) -> World:
    return build_world(admin_conn)


def _theirs(conn: Conn, tenant: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    p = f.product(conn, tenant, category="tshirts")
    back = f.image(conn, tenant, p, role="back", storage_key=f"{tenant}/{p}/images/back.jpg")
    chart = f.image(
        conn,
        tenant,
        p,
        slot="size_chart",
        storage_key=f"{tenant}/{p}/images/chart.jpg",
        clean_content_hash=HASH,
        clean_mime_type="image/jpeg",
        metadata={"size_chart_read": {"status": "read", "source_hash": HASH, "job_id": "x"}},
    )
    return p, back, chart


def _row(conn: Conn, image_id: uuid.UUID) -> tuple[Any, ...]:
    found = conn.execute("SELECT role, metadata FROM images WHERE id = %s", [image_id]).fetchone()
    assert found is not None
    return tuple(found)


async def test_iso_role_and_chart_reads_and_writes(world: World, admin_conn: Conn) -> None:
    from app.repositories.uow import tenant_uow
    from app.services.products import size_chart_suggestion

    acme, bharat = world.tenant("acme"), world.tenant("bharat")
    p, back, chart = _theirs(admin_conn, bharat)
    before = (_row(admin_conn, back), _row(admin_conn, chart))
    async with tenant_uow(acme) as uow:
        assert await uow.images.clear_role(p, None, "back") == []
        assert await uow.images.live_size_chart(p) is None
        assert await uow.images.size_chart_of(p, chart) is None
        assert await uow.images.details_for_product(p) == []
        assert await uow.images.listing_set(p) == []
        written = await uow.images.write_size_chart_read(
            chart, {"status": "failed"}, source_hash=HASH, job_id=uuid.uuid4()
        )
        assert not written
        assert await uow.products.get_by_id(p) is None
        assert await uow.job_runs.latest_for_entity("size_chart_read", chart) is None
        own = f.product(admin_conn, acme)
        mine = await uow.products.get_by_id(own)
        assert mine is not None
        assert await size_chart_suggestion(uow, mine, ()) is None
    assert (_row(admin_conn, back), _row(admin_conn, chart)) == before


async def test_iso_role_write_through_another_tenant_refused(
    world: World, admin_conn: Conn
) -> None:
    from app.db.guard import CrossTenantQueryError
    from app.repositories.images import ImageRepository
    from app.repositories.transactions import unit_of_work

    acme, bharat = world.tenant("acme"), world.tenant("bharat")
    p, back, chart = _theirs(admin_conn, bharat)
    async with unit_of_work(acme) as session:
        repo = ImageRepository(session, bharat)
        with pytest.raises(CrossTenantQueryError):
            await repo.clear_role(p, None, "back")
        with pytest.raises(CrossTenantQueryError):
            await repo.write_size_chart_read(
                chart, {"status": "failed"}, source_hash=HASH, job_id=uuid.uuid4()
            )
    assert _row(admin_conn, back)[0] == "back"


async def test_iso_confirm_with_another_tenants_chart_changes_nothing(
    world: World, admin_conn: Conn
) -> None:
    from app.core.errors import InputError
    from app.core.rbac import Principal
    from app.services.product_edit import update_product
    from app.services.products import product_etag

    acme, bharat = world.tenant("acme"), world.tenant("bharat")
    _, _, chart = _theirs(admin_conn, bharat)
    mine = f.product(admin_conn, acme)
    found = admin_conn.execute("SELECT updated_at FROM products WHERE id = %s", [mine]).fetchone()
    assert found is not None
    owner = world.user("owner@acme.test")
    principal = Principal(
        user_id=owner, tenant_id=acme, role="owner", email="owner@acme.test", jti="iso-si12"
    )
    with pytest.raises(InputError) as caught:
        await update_product(
            principal,
            mine,
            {"size_chart": CHART, "size_chart_image_id": chart},
            product_etag(mine, found[0]),  # type: ignore[arg-type]
        )
    assert caught.value.field == "size_chart_image_id"
    attributes = admin_conn.execute(
        "SELECT attributes FROM products WHERE id = %s", [mine]
    ).fetchone()
    assert attributes is not None and "size_measurements" not in attributes[0]  # type: ignore[operator]
