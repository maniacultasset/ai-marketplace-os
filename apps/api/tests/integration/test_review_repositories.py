"""S19 T3 (AC 1, 6, 12): `BatchRepository`, the approve/reject decision writers and the audit
history read, as `app_rw`."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from app.core.errors import ConflictError

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world
from .review_support import OWNER, REVIEWER, Made, generated

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]
DIGEST = b"\x01" * 32


async def _in_uow(world: GenWorld, email: str, fn: Any) -> Any:
    from app.repositories.uow import tenant_uow

    principal = world.principal(email)
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        return await fn(uow, principal)


async def _batch(world: GenWorld, kind: str = "approve", count: int = 1) -> uuid.UUID:
    async def go(uow: Any, p: Any) -> uuid.UUID:
        return (await uow.batches.insert(kind, count, p.user_id)).id  # type: ignore[no-any-return]

    return await _in_uow(world, REVIEWER, go)  # type: ignore[no-any-return]


async def test_batch_insert_kind_check(gen_world: GenWorld) -> None:
    assert await _batch(gen_world, "approve")
    assert await _batch(gen_world, "reject")
    with pytest.raises(ConflictError):
        await _batch(gen_world, "bogus")


@pytest.mark.parametrize(("count", "ok"), [(0, False), (1, True), (200, True), (201, False)])
async def test_batch_item_count_bounds(gen_world: GenWorld, count: int, ok: bool) -> None:
    if ok:
        assert await _batch(gen_world, count=count)
    else:
        with pytest.raises(ConflictError):
            await _batch(gen_world, count=count)


async def _approve_row(world: GenWorld, made: Made, batch: uuid.UUID) -> uuid.UUID:
    async def go(uow: Any, _p: Any) -> uuid.UUID:
        return await uow.review_decisions.insert_approve(  # type: ignore[no-any-return]
            made.listing_id, made.version_id, batch_id=batch, facts_digest=DIGEST
        )

    return await _in_uow(world, REVIEWER, go)  # type: ignore[no-any-return]


async def test_insert_approve_one_per_version(gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    batch = await _batch(gen_world)
    assert await _approve_row(gen_world, made, batch)
    with pytest.raises(ConflictError):
        await _approve_row(gen_world, made, batch)


async def test_insert_reject_needs_reason_and_feedback(gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    batch = await _batch(gen_world, "reject")

    async def go(uow: Any, _p: Any) -> None:
        await uow.review_decisions.insert_reject(
            made.listing_id, made.version_id, reason_code=None, feedback="x", batch_id=batch
        )

    with pytest.raises(ConflictError):  # ck_review_decisions_reject_reason
        await _in_uow(gen_world, REVIEWER, go)


async def test_decided_at_is_tx_now(gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    batch = await _batch(gen_world, "reject")

    async def go(uow: Any, _p: Any) -> Any:
        from sqlalchemy import text

        await uow.review_decisions.insert_reject(
            made.listing_id,
            made.version_id,
            reason_code="poor_quality",
            feedback="Too generic.",
            batch_id=batch,
        )
        return (await uow.session.execute(text("SELECT now()"))).scalar_one()

    now = await _in_uow(gen_world, REVIEWER, go)
    row = gen_world.conn.execute(
        "SELECT decided_at, decided_by_user_id, batch_id FROM review_decisions "
        "WHERE listing_id = %s AND decision = 'reject'",
        [made.listing_id],
    ).fetchone()
    assert row == (now, gen_world.user(REVIEWER), batch)


def _audit(world: GenWorld, slug: str, listing_id: uuid.UUID, n: int) -> None:
    """`n` audit rows for one listing, one second apart (admin login, the `audit()` function)."""
    for i in range(n):
        world.conn.execute(
            "INSERT INTO audit_log (tenant_id, actor_type, action, entity_type, entity_id, "
            "occurred_at) VALUES (%s, 'system', %s, 'listing', %s, "
            "'2026-10-01T09:00:00Z'::timestamptz + make_interval(secs => %s))",
            [world.tenant(slug), f"listing.test_{i}", listing_id, i],
        )


async def _history(world: GenWorld, listing_id: uuid.UUID, after: Any, limit: int) -> Any:
    async def go(uow: Any, _p: Any) -> Any:
        rows, more = await uow.audit_log.page_for_listing(listing_id, after=after, limit=limit)
        return [(r.occurred_at, r.id, r.action) for r in rows], more

    return await _in_uow(world, OWNER, go)


async def test_audit_page_newest_first_keyset(gen_world: GenWorld) -> None:
    listing = uuid.uuid4()
    _audit(gen_world, "acme", listing, 5)
    first, more = await _history(gen_world, listing, None, 3)
    assert [a for _, _, a in first] == ["listing.test_4", "listing.test_3", "listing.test_2"]
    assert more is True
    second, more = await _history(gen_world, listing, first[-1][:2], 3)
    assert [a for _, _, a in second] == ["listing.test_1", "listing.test_0"]
    assert more is False


async def test_audit_page_other_listing_absent(gen_world: GenWorld) -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    _audit(gen_world, "acme", a, 2)
    _audit(gen_world, "acme", b, 1)
    rows, _ = await _history(gen_world, a, None, 50)
    assert len(rows) == 2


async def test_audit_page_other_tenant_absent(gen_world: GenWorld) -> None:
    listing = uuid.uuid4()
    _audit(gen_world, "bharat", listing, 2)
    rows, more = await _history(gen_world, listing, None, 50)
    assert rows == [] and more is False
