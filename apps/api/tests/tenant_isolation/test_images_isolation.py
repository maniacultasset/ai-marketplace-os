"""S17b T17 (DW2, AC2, AC13; CANON.md:137): the image repository as `app_rw` on `mos_iso`: every
read with another tenant's ids is empty, a write for another tenant is refused, and RLS hides
the other tenant's rows from raw SQL."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from tests.integration import factories as f
from tests.integration.auth_support import World, build_world

pytestmark = pytest.mark.usefixtures("iso_settings")
Conn = psycopg.Connection[tuple[object, ...]]


@pytest.fixture
def world(clean_tenants: None, admin_conn: Conn) -> World:
    return build_world(admin_conn)


def _image(conn: Conn, tenant: uuid.UUID, **cols: Any) -> tuple[uuid.UUID, uuid.UUID, str]:
    from app.services.image_keys import image_object_key

    p = f.product(conn, tenant, category="kurtas")
    image_id = uuid.uuid4()
    key = image_object_key(tenant, p, image_id)
    f.image(conn, tenant, p, id=image_id, slot="main", storage_key=key, **cols)
    return p, image_id, key


async def test_iso_images_repository_reads(world: World, admin_conn: Conn) -> None:
    from app.repositories.uow import tenant_uow

    acme, bharat = world.tenant("acme"), world.tenant("bharat")
    _image(admin_conn, acme)
    p, image_id, key = _image(
        admin_conn, bharat, image_report_json={"amazon": {"pass": True}}, compliance_status="passed"
    )
    async with tenant_uow(acme) as uow:
        repo = uow.images
        assert await repo.get_live(image_id) is None
        assert await repo.get_by_key(key) is None
        assert await repo.count_live(p) == 0
        assert await repo.live_main(p, None) is None
        assert await repo.registered_keys([key]) == set()
        assert await repo.live_keys_for_products([p]) == {p: []}
        assert await repo.live_reports_for_images([image_id]) == {}
        assert await repo.reports_for_product(p, "amazon") == {}
        assert await repo.next_position(p, None, "secondary") == 0  # type: ignore[arg-type]
        assert await repo.write_report(image_id, reports={}, meta=None, content_hash="00") is None
        assert await repo.soft_delete(image_id) is False
    row = admin_conn.execute(
        "SELECT deleted_at, content_hash FROM images WHERE id = %s", [image_id]
    ).fetchone()
    assert row == (None, None)


async def test_iso_images_write_other_tenant_refused(world: World, admin_conn: Conn) -> None:
    from app.db.guard import CrossTenantQueryError
    from app.repositories.images import ImageRepository, NewImage
    from app.repositories.transactions import unit_of_work
    from app.services.image_keys import image_object_key
    from mkos_schemas.enums import ImageSlot

    acme, bharat = world.tenant("acme"), world.tenant("bharat")
    p = f.product(admin_conn, bharat, category="kurtas")
    image_id = uuid.uuid4()
    new = NewImage(
        id=image_id, product_id=p, colourway_id=None, storage_key=image_object_key(bharat, p, image_id),
        slot=ImageSlot.MAIN, bytes=1, position=0,
    )  # fmt: skip
    async with unit_of_work(acme) as session:
        with pytest.raises(CrossTenantQueryError):
            await ImageRepository(session, bharat).create(new)
    assert admin_conn.execute("SELECT count(*) FROM images").fetchone() == (0,)


def test_iso_images_report_rls(world: World, admin_conn: Conn, app_conn: Conn) -> None:
    acme, bharat = world.tenant("acme"), world.tenant("bharat")
    _image(admin_conn, acme, deleted_at=datetime.now(UTC))
    _image(admin_conn, bharat, image_report_json={"amazon": {"pass": False}})
    with app_conn.transaction(force_rollback=True):
        app_conn.execute("SELECT set_config('app.tenant_id', %s, true)", [str(acme)])
        seen = {r[0] for r in app_conn.execute("SELECT tenant_id FROM images").fetchall()}
        reports = app_conn.execute(
            "SELECT count(*) FROM images WHERE image_report_json IS NOT NULL"
        ).fetchone()
    assert seen == {acme}
    assert reports == (0,)
