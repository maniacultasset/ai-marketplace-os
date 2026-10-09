"""S19 T2 (AC 7, 11, 12): `ListingRepository.lock_many` and `page`, as `app_rw`."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world, set_status
from .products_support import statements
from .review_support import Made, generated

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]
T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


def _stamp(world: GenWorld, listing_id: uuid.UUID, at: datetime) -> None:
    """Both order keys of a listing set to `at` (admin login, triggers off: an arrangement)."""
    with world.conn.transaction():
        world.conn.execute("SET LOCAL session_replication_role = replica")
        world.conn.execute(
            "UPDATE listings SET status_changed_at = %s, last_event_at = %s WHERE id = %s",
            [at, at, listing_id],
        )


async def _three(world: GenWorld) -> list[Made]:
    made = [await generated(world) for _ in range(3)]
    for i, m in enumerate(made):
        _stamp(world, m.listing_id, T0 + timedelta(minutes=i))
    return made


Keys = list[tuple[datetime, datetime, uuid.UUID]]


async def _page(
    world: GenWorld, filters: Any, after: Any = None, limit: int = 50
) -> tuple[list[uuid.UUID], bool, Keys]:
    from app.repositories.uow import tenant_uow

    principal = world.principal("reviewer@acme.test")
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        rows, more = await uow.listings.page(filters, after=after, limit=limit)
        return (
            [r.id for r in rows],
            more,
            [(r.status_changed_at, r.last_event_at, r.id) for r in rows],
        )


async def _lock(
    world: GenWorld, ids: list[uuid.UUID], slug: str = "acme"
) -> dict[uuid.UUID, object]:
    from app.repositories.uow import tenant_uow

    principal = world.principal(f"reviewer@{slug}.test")
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        return dict(await uow.listings.lock_many(ids))


async def test_lock_many_id_order(gen_world: GenWorld) -> None:
    made = await _three(gen_world)
    ids = [m.listing_id for m in reversed(made)]
    with statements() as seen:
        locked = await _lock(gen_world, ids)
    assert set(locked) == set(ids)
    sql = [s for s in seen if "FROM listings" in s and "FOR UPDATE" in s]
    assert len(sql) == 1
    assert "ORDER BY listings.id" in sql[0]


async def test_lock_many_missing_ids_absent(gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    locked = await _lock(gen_world, [made.listing_id, uuid.uuid4()])
    assert set(locked) == {made.listing_id}
    assert await _lock(gen_world, []) == {}


async def test_lock_many_other_tenant_absent(gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    assert await _lock(gen_world, [made.listing_id], slug="bharat") == {}


async def test_page_review_order_oldest_first(gen_world: GenWorld) -> None:
    from app.repositories.listings import ListingFilter

    made = await _three(gen_world)
    ids, more, _ = await _page(gen_world, ListingFilter(status="validated"))
    mine = [i for i in ids if i in {m.listing_id for m in made}]
    assert mine == [m.listing_id for m in made]
    assert more is False


async def test_page_other_order_newest_first(gen_world: GenWorld) -> None:
    from app.repositories.listings import ListingFilter

    made = await _three(gen_world)
    ids, _, _ = await _page(gen_world, ListingFilter())
    mine = [i for i in ids if i in {m.listing_id for m in made}]
    assert mine == [m.listing_id for m in reversed(made)]


async def test_page_two_pages_no_dup_no_gap(gen_world: GenWorld) -> None:
    from app.repositories.listings import ListingFilter

    made = [await generated(gen_world) for _ in range(4)]
    for m in made:
        _stamp(gen_world, m.listing_id, T0)  # equal timestamps: the id breaks the tie
    for filters in (ListingFilter(status="validated"), ListingFilter(marketplace="amazon")):
        seen: list[uuid.UUID] = []
        after = None
        while True:
            ids, more, keys = await _page(gen_world, filters, after, limit=2)
            seen += ids
            if not more:
                break
            last = keys[-1]
            after = (last[0] if filters.queue else last[1], last[2])
        assert len(seen) == len(set(seen))
        assert {m.listing_id for m in made} <= set(seen)


async def test_page_filters_combine(gen_world: GenWorld) -> None:
    from app.repositories.listings import ListingFilter

    a, b = await generated(gen_world), await generated(gen_world)
    set_status(gen_world, b.listing_id, "needs_review")
    product_a = gen_world.listing(a.listing_id)["product_id"]
    ids, _, _ = await _page(
        gen_world, ListingFilter(status="validated", marketplace="amazon", product_id=product_a)
    )
    assert ids == [a.listing_id]
    ids, _, _ = await _page(gen_world, ListingFilter(status="needs_review"))
    assert b.listing_id in ids and a.listing_id not in ids
    ids, _, _ = await _page(gen_world, ListingFilter(marketplace="flipkart"))
    assert a.listing_id not in ids


async def test_page_queue_excludes_paused_archived(gen_world: GenWorld) -> None:
    from app.repositories.listings import ListingFilter

    made = await _three(gen_world)
    with gen_world.conn.transaction():
        gen_world.conn.execute("SET LOCAL session_replication_role = replica")
        gen_world.conn.execute(
            "UPDATE listings SET paused_at = now() WHERE id = %s", [made[0].listing_id]
        )
        gen_world.conn.execute(
            "UPDATE listings SET archived_at = now() WHERE id = %s", [made[1].listing_id]
        )
    ids, _, _ = await _page(gen_world, ListingFilter(status="validated"))
    assert made[2].listing_id in ids
    assert made[0].listing_id not in ids and made[1].listing_id not in ids
    ids, _, _ = await _page(gen_world, ListingFilter())
    assert {m.listing_id for m in made} <= set(ids)  # the all-status view keeps them
