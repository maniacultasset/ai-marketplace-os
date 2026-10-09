"""S18 T3 (AC 1, 10, 11; DATABASE.md:740-789): `ListingRepository` and `ListingVersionRepository`
as `app_rw`."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from . import factories as f
from .conftest import LOCAL_HOSTS

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)


@pytest.fixture
def world(products_world: Any) -> Any:
    return products_world


def _uow(tenant: uuid.UUID, user: uuid.UUID | None = None) -> Any:
    from app.repositories.uow import tenant_uow

    return tenant_uow(tenant, user_id=user)


async def test_create_then_get(world: Any) -> None:
    acme = world.tenant("acme")
    p = f.product(world.conn, acme)
    async with _uow(acme) as uow:
        created = await uow.listings.create(p, "amazon", "t_shirts")
    async with _uow(acme) as uow:
        got = await uow.listings.get(created.id)
    assert got is not None
    assert (got.status, got.last_event, got.product_id) == ("created", "create", p)


async def test_pair_unique_two_sessions_one_wins(world: Any) -> None:
    from app.core.errors import ConflictError
    from mkos_schemas.codes import ErrorCode

    acme = world.tenant("acme")
    p = f.product(world.conn, acme)
    started = asyncio.Event()

    async def first() -> None:
        async with _uow(acme) as uow:
            await uow.listings.create(p, "amazon", "t_shirts")
            started.set()
            await asyncio.sleep(0.3)

    async def second() -> None:
        await started.wait()
        async with _uow(acme) as uow:
            await uow.listings.create(p, "amazon", "t_shirts")

    results = await asyncio.gather(first(), second(), return_exceptions=True)
    assert results[0] is None
    assert isinstance(results[1], ConflictError) and results[1].code is ErrorCode.LISTING_BUSY
    row = world.conn.execute("SELECT count(*) FROM listings WHERE product_id = %s", [p]).fetchone()
    assert row[0] == 1


async def test_get_for_update_blocks_second_locker(world: Any) -> None:
    from sqlalchemy import text

    acme = world.tenant("acme")
    listing = f.listing(world.conn, acme)
    async with _uow(acme) as one:
        assert await one.listings.get_for_update(listing) is not None
        async with _uow(acme) as two:
            await two.session.execute(text("SET LOCAL lock_timeout = '200ms'"))
            with pytest.raises(Exception) as caught:
                await two.listings.get_for_update(listing)
    assert "lock timeout" in str(caught.value)


async def test_next_version_under_lock(world: Any) -> None:
    acme = world.tenant("acme")
    listing = f.listing(world.conn, acme)
    async with _uow(acme) as uow:
        assert await uow.listing_versions.next_version(listing) == 1
    f.version(world.conn, acme, listing, version=1)
    f.version(world.conn, acme, listing, version=4)
    async with _uow(acme) as uow:
        assert await uow.listing_versions.next_version(listing) == 5


async def test_insert_edit_needs_changed_paths(world: Any) -> None:
    from app.core.errors import ConflictError
    from app.repositories.listings import NewVersion

    acme = world.tenant("acme")
    owner = world.user("owner@acme.test")
    listing = f.listing(world.conn, acme)
    with pytest.raises(ConflictError):
        async with _uow(acme) as uow:
            await uow.listing_versions.insert(
                NewVersion(
                    listing_id=listing,
                    version=1,
                    content_json={"title": "x"},
                    generated_by_user_id=owner,
                    origin="edit",
                    changed_paths=[],
                )
            )


async def test_version_immutable_app_rw(world: Any) -> None:
    from sqlalchemy import text

    acme = world.tenant("acme")
    listing = f.listing(world.conn, acme)
    version = f.version(world.conn, acme, listing)
    with pytest.raises(Exception) as caught:
        async with _uow(acme) as uow:
            await uow.session.execute(
                text("UPDATE listing_versions SET content_json = '{}'::jsonb WHERE id = :v"),
                {"v": version},
            )
    assert (
        "42501" in str(caught.value)
        or "GRD01" in str(caught.value)
        or "permission" in str(caught.value)
    )


async def test_version_other_listing_404(world: Any) -> None:
    acme = world.tenant("acme")
    one = f.listing(world.conn, acme)
    two = f.listing(world.conn, acme)
    v2 = f.version(world.conn, acme, two)
    async with _uow(acme) as uow:
        assert await uow.listing_versions.get(one, v2) is None
        assert await uow.listing_versions.get(two, v2) is not None


async def test_other_tenant_none(world: Any) -> None:
    bharat = world.tenant("bharat")
    listing = f.listing(world.conn, bharat)
    version = f.version(world.conn, bharat, listing)
    p = world.conn.execute("SELECT product_id FROM listings WHERE id = %s", [listing]).fetchone()[0]
    async with _uow(world.tenant("acme")) as uow:
        assert await uow.listings.get(listing) is None
        assert await uow.listings.get_for_update(listing) is None
        assert await uow.listings.get_by_pair_for_update(p, "amazon") is None
        assert await uow.listing_versions.get(listing, version) is None
