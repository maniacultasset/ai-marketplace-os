"""SI1 T22 (CP9): the tenant purge step deletes every public copy before `purge_tenant`, and the
0015 delete guard refuses the purge while a public name is left. On the compose MinIO. Run in
the Docker batch (`make api-tests-integration`)."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from app.adapters.storage import ObjectStorage, StorageUnavailable, get_storage
from app.db.image_purge import purge_public_images
from app.services.public_images import PUBLIC_CACHE_CONTROL, new_public_id
from sqlalchemy.ext.asyncio import create_async_engine

from . import factories as f
from .conftest import LOCAL_HOSTS, Conn, expect_sqlstate
from .integrity_support import as_migrator
from .public_images_support import PUBLIC_BUCKET, public_hosting

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["public_hosting"]
FORCE_DATA = "SELECT purge_tenant(%s, 'data', force => true)"


async def _published(conn: Conn, tenant: uuid.UUID, *, deleted: bool, mime: str | None) -> str:
    p = f.product(conn, tenant)
    public_id = new_public_id()
    f.image(
        conn,
        tenant,
        p,
        clean_content_hash="d" * 64,
        clean_mime_type="image/jpeg",
        public_id=public_id,
    )
    if deleted:
        conn.execute(
            "UPDATE images SET deleted_at = now(), clean_content_hash = NULL, "
            "clean_mime_type = NULL WHERE public_id = %s",
            [public_id],
        )
    await get_storage().put(
        PUBLIC_BUCKET,
        f"{public_id}.jpg",
        b"\xff\xd8\xffpub",
        "image/jpeg",
        cache_control=PUBLIC_CACHE_CONTROL,
    )
    assert mime is None or mime
    return public_id


async def _purge_step(tenant: uuid.UUID) -> int:
    from app.core.config import get_settings

    engine = create_async_engine(str(get_settings().alembic_database_url))
    try:
        async with engine.begin() as connection:
            return await purge_public_images(connection, get_storage(), tenant)
    finally:
        await engine.dispose()


async def test_purged_tenant_public_url_404(auth_db: Conn, public_hosting: str) -> None:
    tenant = f.tenant(auth_db)
    live = await _published(auth_db, tenant, deleted=False, mime="image/jpeg")
    tomb = await _published(auth_db, tenant, deleted=True, mime=None)
    f.image(auth_db, tenant, f.product(auth_db, tenant))  # a live image with no copy
    other = f.tenant(auth_db)
    theirs = await _published(auth_db, other, deleted=False, mime="image/jpeg")
    assert await _purge_step(tenant) == 2
    left = auth_db.execute(
        "SELECT count(*) FROM images WHERE tenant_id = %s AND public_id IS NOT NULL", [tenant]
    ).fetchone()
    assert left == (0,)
    with as_migrator(auth_db):
        auth_db.execute(FORCE_DATA, (tenant,))
    async with httpx.AsyncClient() as anonymous:
        for public_id in (live, tomb):
            assert (await anonymous.get(f"{public_hosting}/{public_id}.jpg")).status_code in (
                403,
                404,
            )
        assert (await anonymous.get(f"{public_hosting}/{theirs}.jpg")).status_code == 200


async def test_purge_refused_without_the_step(auth_db: Conn, public_hosting: str) -> None:
    tenant = f.tenant(auth_db)
    await _published(auth_db, tenant, deleted=False, mime="image/jpeg")
    with as_migrator(auth_db):
        expect_sqlstate(auth_db, "GRD01", FORCE_DATA, (tenant,))
    rows = auth_db.execute("SELECT count(*) FROM images WHERE tenant_id = %s", [tenant]).fetchone()
    assert rows == (1,)


async def test_store_failure_keeps_the_names(
    auth_db: Conn, public_hosting: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = f.tenant(auth_db)
    await _published(auth_db, tenant, deleted=False, mime="image/jpeg")

    async def down(self: Any, bucket: str, key: str) -> None:
        raise StorageUnavailable("down")

    monkeypatch.setattr(ObjectStorage, "delete", down)
    assert await _purge_step(tenant) == 0
    left = auth_db.execute(
        "SELECT count(*) FROM images WHERE tenant_id = %s AND public_id IS NOT NULL", [tenant]
    ).fetchone()
    assert left == (1,)


async def test_reset_seed_tenants_leaves_no_public_object(
    auth_db: Conn, public_hosting: str
) -> None:
    from app.core.config import get_settings
    from app.db.seed_ops import reset_seed_tenants, seed_tenant_id

    acme = seed_tenant_id("acme")
    exists = auth_db.execute("SELECT count(*) FROM tenants WHERE id = %s", [acme]).fetchone()
    if exists == (0,):
        pytest.skip("the seed tenants are not in this database")
    public_id = await _published(auth_db, acme, deleted=False, mime="image/jpeg")
    engine = create_async_engine(str(get_settings().alembic_database_url))
    try:
        async with engine.begin() as connection:
            await reset_seed_tenants(connection)
    finally:
        await engine.dispose()
    assert await get_storage().head(PUBLIC_BUCKET, f"{public_id}.jpg") is None
