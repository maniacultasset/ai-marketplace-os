"""S18 T5 (AC 1, 15; invariant 1; DATABASE.md:1379-1402): `ListingRepository.move` with user and
worker actors against the 0004 status guards, as `app_rw`."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from . import factories as f
from .conftest import LOCAL_HOSTS
from .jobs_support import insert_job

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)


@pytest.fixture
def world(products_world: Any) -> Any:
    return products_world


def _uow(tenant: uuid.UUID, user: uuid.UUID | None = None) -> Any:
    from app.repositories.uow import tenant_uow

    return tenant_uow(tenant, user_id=user)


def _queued_job(conn: Any, tenant: uuid.UUID, listing: uuid.UUID) -> uuid.UUID:
    return insert_job(
        conn,
        tenant,
        kind="listing_generate",
        entity_type="listing",
        entity_id=listing,
        listing_id=listing,
    )


async def _move(tenant: uuid.UUID, listing: uuid.UUID, actor: Any, **kw: Any) -> Any:
    user = getattr(actor, "user_id", None)
    async with _uow(tenant, user) as uow:
        row = await uow.listings.get_for_update(listing)
        assert row is not None
        return await uow.listings.move(row, actor=actor, **kw)


async def _generating(world: Any, tenant: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    from app.repositories.listings import UserActor

    listing = f.listing(world.conn, tenant)
    job = _queued_job(world.conn, tenant, listing)
    await _move(
        tenant,
        listing,
        UserActor(world.user("operator@acme.test")),
        to="generating",
        event="generate",
    )
    return listing, job


def _status(conn: Any, listing: uuid.UUID) -> tuple[str, str | None, str]:
    row = conn.execute(
        "SELECT status, status_reason, last_event FROM listings WHERE id = %s", [listing]
    ).fetchone()
    return row[0], row[1], row[2]


async def test_move_generate_needs_queued_job(world: Any) -> None:
    from app.core.errors import ConflictError
    from app.repositories.listings import UserActor
    from mkos_schemas.codes import ErrorCode

    acme = world.tenant("acme")
    listing = f.listing(world.conn, acme)
    with pytest.raises(ConflictError) as caught:
        await _move(
            acme,
            listing,
            UserActor(world.user("operator@acme.test")),
            to="generating",
            event="generate",
        )
    assert caught.value.code is ErrorCode.INVALID_STATUS_TRANSITION
    _queued_job(world.conn, acme, listing)
    await _move(
        acme,
        listing,
        UserActor(world.user("operator@acme.test")),
        to="generating",
        event="generate",
    )
    assert _status(world.conn, listing) == ("generating", None, "generate")


async def test_move_worker_edge_needs_job_actor(world: Any) -> None:
    from app.core.errors import ConflictError
    from app.repositories.listings import UserActor
    from mkos_schemas.codes import ErrorCode

    acme = world.tenant("acme")
    listing, _job = await _generating(world, acme)
    with pytest.raises(ConflictError) as caught:
        await _move(
            acme,
            listing,
            UserActor(world.user("operator@acme.test")),
            to="validated",
            event="generation_clean",
        )
    assert caught.value.code is ErrorCode.INVALID_STATUS_TRANSITION
    assert _status(world.conn, listing)[0] == "generating"


async def test_move_worker_edge_with_job_actor_ok(world: Any) -> None:
    from app.repositories.listings import JobActor

    acme = world.tenant("acme")
    listing, job = await _generating(world, acme)
    await _move(acme, listing, JobActor(job), to="validated", event="generation_clean")
    assert _status(world.conn, listing) == ("validated", None, "generation_clean")


async def test_move_generation_failed_reason_required(world: Any) -> None:
    from app.core.errors import ConflictError
    from app.repositories.listings import JobActor

    acme = world.tenant("acme")
    listing, job = await _generating(world, acme)
    with pytest.raises(ConflictError):
        await _move(acme, listing, JobActor(job), to="failed", event="generation_failed")
    await _move(
        acme,
        listing,
        JobActor(job),
        to="failed",
        event="generation_failed",
        reason="generation_failed",
    )
    assert _status(world.conn, listing) == ("failed", "generation_failed", "generation_failed")


@pytest.mark.parametrize(
    ("column", "code"), [("paused", "LISTING_PAUSED"), ("archived", "LISTING_ARCHIVED")]
)
async def test_move_paused_or_archived_listing_409(world: Any, column: str, code: str) -> None:
    from app.core.errors import ConflictError
    from app.repositories.listings import UserActor

    acme = world.tenant("acme")
    owner = world.user("owner@acme.test")
    listing = f.listing(
        world.conn, acme, **{f"{column}_at": "2026-10-01T00:00:00Z", f"{column}_by_user_id": owner}
    )
    _queued_job(world.conn, acme, listing)
    with pytest.raises(ConflictError) as caught:
        await _move(acme, listing, UserActor(owner), to="generating", event="generate")
    assert caught.value.code.value == code


@pytest.mark.parametrize("target", ["approved", "publishing", "published"])
async def test_move_never_to_approved_or_publishing_from_generation(
    world: Any, target: str
) -> None:
    from app.core.errors import ConflictError
    from app.repositories.listings import JobActor

    acme = world.tenant("acme")
    listing, job = await _generating(world, acme)
    with pytest.raises(ConflictError):
        await _move(acme, listing, JobActor(job), to=target, event="generation_clean")
    assert _status(world.conn, listing)[0] == "generating"


async def test_bind_job_actor_local_to_transaction(world: Any) -> None:
    from app.repositories.listings import JobActor
    from sqlalchemy import text

    acme = world.tenant("acme")
    listing, job = await _generating(world, acme)
    async with _uow(acme) as uow:
        row = await uow.listings.get_for_update(listing)
        assert row is not None
        await uow.listings.move(row, actor=JobActor(job), to="validated", event="generation_clean")
        inside = (
            await uow.session.execute(text("SELECT current_setting('app.job_id', true)"))
        ).scalar()
    async with _uow(acme) as uow:
        after = (
            await uow.session.execute(text("SELECT current_setting('app.job_id', true)"))
        ).scalar()
    assert inside in ("", None) and after in ("", None)


async def test_move_user_actor_needs_that_users_session(world: Any) -> None:
    from app.repositories.listings import UserActor

    acme = world.tenant("acme")
    listing = f.listing(world.conn, acme)
    _queued_job(world.conn, acme, listing)
    with pytest.raises(ValueError, match="user"):
        await _move_as(acme, listing, UserActor(world.user("owner@acme.test")), None)


async def _move_as(tenant: uuid.UUID, listing: uuid.UUID, actor: Any, user: Any) -> None:
    async with _uow(tenant, user) as uow:
        row = await uow.listings.get_for_update(listing)
        assert row is not None
        await uow.listings.move(row, actor=actor, to="generating", event="generate")
