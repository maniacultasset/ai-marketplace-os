"""SI4 A12 (AC9-AC11; CP-D12; D19): the fact and image questions of the issue inbox, as `app_rw`:
one live task per fact or image, reuse under a row lock, and a cancel that only fires when every
linked issue is closed."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest

from . import factories as f
from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world
from .review_support import Made, generated

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]
TARGET = "facts.attributes.fiber_composition"
QUESTION = "Add the fiber composition on the product."
REASON = "The marketplace needs the fiber composition to list this product."
ANSWER: dict[str, Any] = {"kind": "percent_composition"}


async def _uow(world: GenWorld, fn: Callable[[Any], Awaitable[Any]], slug: str = "acme") -> Any:
    from app.repositories.uow import tenant_uow

    async with tenant_uow(world.tenant(slug)) as uow:
        return await fn(uow)


async def _open_fact(world: GenWorld, product: uuid.UUID, **kw: Any) -> uuid.UUID:
    async def go(uow: Any) -> uuid.UUID:
        return await uow.human_tasks.open_fact_task(  # type: ignore[no-any-return]
            product, kw.pop("target", TARGET), QUESTION, REASON, dict(ANSWER), **kw
        )

    return await _uow(world, go)  # type: ignore[no-any-return]


async def _open_image(world: GenWorld, product: uuid.UUID, image: uuid.UUID) -> uuid.UUID:
    async def go(uow: Any) -> uuid.UUID:
        return await uow.human_tasks.open_image_task(  # type: ignore[no-any-return]
            product, image, "Upload a new main image.", "The image failed its checks."
        )

    return await _uow(world, go)  # type: ignore[no-any-return]


async def _cancel(world: GenWorld, task: uuid.UUID, slug: str = "acme") -> bool:
    async def go(uow: Any) -> bool:
        return await uow.human_tasks.cancel_if_closed(task)  # type: ignore[no-any-return]

    return await _uow(world, go, slug)  # type: ignore[no-any-return]


def _task(world: GenWorld, task: uuid.UUID) -> tuple[Any, ...]:
    row = world.conn.execute(
        "SELECT status, cancelled_reason FROM human_tasks WHERE id = %s", [task]
    ).fetchone()
    assert row is not None
    return tuple(row)


def _live_tasks(world: GenWorld, product: uuid.UUID) -> int:
    return world.count(
        "human_tasks", "product_id = %s AND status IN ('open', 'answered')", [product]
    )


def _issue(world: GenWorld, made: Made, task: uuid.UUID | None, status: str = "open") -> uuid.UUID:
    tenant = world.tenant("acme")
    columns: dict[str, Any] = {
        "human_task_id": task,
        "dedupe_key": f"rules_engine|content.title|FIELD_TOO_LONG|{uuid.uuid4()}",
    }
    if status != "open":
        columns.update(status=status, resolution="regenerated", resolved_at="now()")
    if status == "auto_fixed":
        columns.update(resolution="deterministic_fix")
    return f.listing_issue(world.conn, tenant, made.listing_id, **columns)


def _product(world: GenWorld, made: Made) -> uuid.UUID:
    return world.scalar(  # type: ignore[no-any-return]
        "SELECT product_id FROM listings WHERE id = %s", [made.listing_id]
    )


# ---- open_fact_task ----


async def test_open_fact_task_one_row_per_fact(gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    product = _product(gen_world, made)
    first = await _open_fact(gen_world, product)
    second = await _open_fact(gen_world, product)
    assert first == second
    assert _live_tasks(gen_world, product) == 1
    row = gen_world.conn.execute(
        "SELECT kind, target_path, resume_from, expected_answer FROM human_tasks WHERE id = %s",
        [first],
    ).fetchone()
    assert row == ("missing_fact", TARGET, "generate", ANSWER)


async def test_open_fact_task_two_listings_of_one_product(gen_world: GenWorld) -> None:
    amazon = await generated(gen_world)
    sku = gen_world.scalar(
        "SELECT p.sku FROM products p JOIN listings l ON l.product_id = p.id WHERE l.id = %s",
        [amazon.listing_id],
    )
    flipkart = await generated(gen_world, sku=sku, marketplace="flipkart")
    product = _product(gen_world, amazon)
    assert _product(gen_world, flipkart) == product
    assert await _open_fact(gen_world, product) == await _open_fact(gen_world, product)
    assert _live_tasks(gen_world, product) == 1


async def test_open_fact_task_per_colourway(gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    product = _product(gen_world, made)
    tenant = gen_world.tenant("acme")
    style = f.product(gen_world.conn, tenant, "style")
    colour = f.colourway(gen_world.conn, tenant, style)
    plain = await _open_fact(gen_world, style)
    coloured = await _open_fact(gen_world, style, colourway_id=colour)
    assert plain != coloured
    assert _live_tasks(gen_world, style) == 2
    assert _live_tasks(gen_world, product) == 0


async def test_open_fact_task_reuses_a_task_another_lane_opened(gen_world: GenWorld) -> None:
    """D19(a): a live task at the same target with no linked issue (SI3's photo question) is
    the one returned."""
    made = await generated(gen_world)
    product = _product(gen_world, made)
    lane = f.human_task(
        gen_world.conn, gen_world.tenant("acme"), product_id=product, target_path=TARGET
    )
    assert await _open_fact(gen_world, product) == lane


@pytest.mark.parametrize("size", [4, 501])
async def test_question_length_backstop(gen_world: GenWorld, size: int) -> None:
    from app.core.errors import ConflictError

    made = await generated(gen_world)
    product = _product(gen_world, made)

    async def go(uow: Any) -> Any:
        return await uow.human_tasks.open_fact_task(product, TARGET, "q" * size, REASON, ANSWER)

    with pytest.raises((psycopg.Error, ConflictError, Exception)) as caught:
        await _uow(gen_world, go)
    assert "ck_human_tasks_question" in str(caught.value) or "23514" in repr(caught.value)


# ---- open_image_task ----


async def test_open_image_task_one_row(gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    product = _product(gen_world, made)
    image = f.image(gen_world.conn, gen_world.tenant("acme"), product, slot="main")
    first = await _open_image(gen_world, product, image)
    assert await _open_image(gen_world, product, image) == first
    row = gen_world.conn.execute(
        "SELECT kind, image_id, target_path, resume_from, expected_answer FROM human_tasks "
        "WHERE id = %s",
        [first],
    ).fetchone()
    assert row == ("image_replacement", image, None, "validate", {"kind": "file"})


async def test_open_image_task_concurrent_sessions_one_row(gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    product = _product(gen_world, made)
    image = f.image(gen_world.conn, gen_world.tenant("acme"), product, slot="main")
    ids = await asyncio.gather(*(_open_image(gen_world, product, image) for _ in range(4)))
    assert len(set(ids)) == 1
    assert gen_world.count("human_tasks", "image_id = %s", [image]) == 1


# ---- cancel_if_closed ----


async def test_cancel_if_closed_rules(gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    product = _product(gen_world, made)
    task = await _open_fact(gen_world, product)
    open_issue = _issue(gen_world, made, task)
    assert await _cancel(gen_world, task) is False
    assert _task(gen_world, task) == ("open", None)
    gen_world.conn.execute(
        "UPDATE listing_issues SET status = 'resolved', resolution = 'regenerated', "
        "resolved_at = now() WHERE id = %s",
        [open_issue],
    )
    assert await _cancel(gen_world, task) is True
    assert _task(gen_world, task) == ("cancelled", "issues_closed")
    assert await _cancel(gen_world, task) is False  # already cancelled


async def test_cancel_if_closed_never_cancels_an_unlinked_task(gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    product = _product(gen_world, made)
    lane = f.human_task(
        gen_world.conn, gen_world.tenant("acme"), product_id=product, target_path=TARGET
    )
    assert await _cancel(gen_world, lane) is False
    assert _task(gen_world, lane) == ("open", None)


async def test_live_task_ids(gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    product = _product(gen_world, made)
    task = await _open_fact(gen_world, product)
    _issue(gen_world, made, task, status="resolved")
    _issue(gen_world, made, task)

    async def go(uow: Any) -> Any:
        return await uow.listing_issues.live_task_ids(made.listing_id)

    assert await _uow(gen_world, go) == [task]
    gen_world.conn.execute(
        "UPDATE human_tasks SET status = 'cancelled', cancelled_reason = 'superseded' "
        "WHERE id = %s",
        [task],
    )
    assert await _uow(gen_world, go) == []


# ---- races (pg_locks proves the row lock) ----


async def _wait_for_lock_wait(world: GenWorld) -> None:
    for _ in range(200):
        waiting = world.scalar(
            "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
            "AND datname = current_database()"
        )
        if waiting:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("the second session never waited on the row lock")


def _row_locked(world: GenWorld, task: uuid.UUID) -> bool:
    """Another backend waits for a lock held on the task's row (a tuple or transaction lock)."""
    return bool(
        world.scalar(
            "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
            "WHERE NOT l.granted AND a.datname = current_database()"
        )
    )


async def test_two_closers_cancel_once(gen_world: GenWorld) -> None:
    from app.repositories.uow import tenant_uow

    made = await generated(gen_world)
    product = _product(gen_world, made)
    task = await _open_fact(gen_world, product)
    _issue(gen_world, made, task, status="resolved")
    release, ready = asyncio.Event(), asyncio.Event()
    results: list[bool] = []

    async def first() -> None:
        async with tenant_uow(gen_world.tenant("acme")) as uow:
            results.append(await uow.human_tasks.cancel_if_closed(task))
            ready.set()
            await release.wait()

    holder = asyncio.create_task(first())
    await ready.wait()
    second = asyncio.create_task(_cancel(gen_world, task))
    await _wait_for_lock_wait(gen_world)
    assert _row_locked(gen_world, task)
    release.set()
    await holder
    results.append(await second)
    assert sorted(results) == [False, True]
    assert _task(gen_world, task) == ("cancelled", "issues_closed")


async def test_opener_holds_the_lock_against_a_closer(gen_world: GenWorld) -> None:
    """B reuses the live task and links its open issue while A tries to cancel: A waits, then
    sees B's link and keeps the task open."""
    from app.repositories.uow import tenant_uow

    made = await generated(gen_world)
    product = _product(gen_world, made)
    task = await _open_fact(gen_world, product)
    _issue(gen_world, made, task, status="resolved")
    new_issue = _issue(gen_world, made, None)
    release, ready = asyncio.Event(), asyncio.Event()
    opened: list[uuid.UUID] = []

    async def opener() -> None:
        async with tenant_uow(gen_world.tenant("acme")) as uow:
            got = await uow.human_tasks.open_fact_task(product, TARGET, QUESTION, REASON, ANSWER)
            await uow.listing_issues.link_ids([new_issue], got)
            opened.append(got)
            ready.set()
            await release.wait()

    holder = asyncio.create_task(opener())
    await ready.wait()
    assert opened == [task]
    closer = asyncio.create_task(_cancel(gen_world, task))
    await _wait_for_lock_wait(gen_world)
    assert _row_locked(gen_world, task)
    release.set()
    await holder
    assert await closer is False
    assert _task(gen_world, task) == ("open", None)
    linked = gen_world.scalar("SELECT human_task_id FROM listing_issues WHERE id = %s", [new_issue])
    assert linked == task


async def test_a_committed_cancel_makes_the_opener_open_a_new_task(gen_world: GenWorld) -> None:
    from app.repositories.uow import tenant_uow

    made = await generated(gen_world)
    product = _product(gen_world, made)
    task = await _open_fact(gen_world, product)
    _issue(gen_world, made, task, status="resolved")
    new_issue = _issue(gen_world, made, None)
    release, ready = asyncio.Event(), asyncio.Event()
    cancelled: list[bool] = []

    async def closer() -> None:
        async with tenant_uow(gen_world.tenant("acme")) as uow:
            cancelled.append(await uow.human_tasks.cancel_if_closed(task))
            ready.set()
            await release.wait()

    holder = asyncio.create_task(closer())
    await ready.wait()

    async def open_and_link(uow: Any) -> uuid.UUID:
        got: uuid.UUID = await uow.human_tasks.open_fact_task(
            product, TARGET, QUESTION, REASON, ANSWER
        )
        await uow.listing_issues.link_ids([new_issue], got)
        return got

    opener = asyncio.create_task(_uow(gen_world, open_and_link))
    await _wait_for_lock_wait(gen_world)
    release.set()
    await holder
    fresh = await opener
    assert cancelled == [True]
    assert fresh != task
    assert _task(gen_world, task) == ("cancelled", "issues_closed")
    assert _task(gen_world, fresh) == ("open", None)
    linked = gen_world.scalar("SELECT human_task_id FROM listing_issues WHERE id = %s", [new_issue])
    assert linked == fresh


# ---- step 4 lock order (round 3 F3): reconcile and open take a product's locks in one order ----

COUNTRY = "facts.attributes.country_of_origin"


def _sibling(world: GenWorld, product: uuid.UUID) -> Any:
    """The product's Flipkart listing beside the generated Amazon one."""
    listing_id = f.listing(world.conn, world.tenant("acme"), product, marketplace="flipkart")
    return SimpleNamespace(id=listing_id, product_id=product)


def _linked(world: GenWorld, listing: Any, task: uuid.UUID, marketplace: str) -> uuid.UUID:
    return f.listing_issue(
        world.conn,
        world.tenant("acme"),
        listing.id,
        marketplace=marketplace,
        human_task_id=task,
        dedupe_key=f"rules_engine|content.title|FIELD_TOO_LONG|{uuid.uuid4()}",
    )


async def _step4_race(
    world: GenWorld,
    a: Any,
    b: Any,
    a_opens: Callable[[Any], Awaitable[uuid.UUID]],
    b_opens: Callable[[Any], Awaitable[uuid.UUID]],
) -> list[uuid.UUID]:
    """A reconciles (holding its linked task rows), B runs its whole step 4 and waits on a
    lock, then A opens its question. With the locks taken in two orders the sessions deadlock
    (40P01) and one of them fails; with one order B waits for A's commit."""
    from app.repositories.uow import tenant_uow
    from app.services.issue_questions import reconcile_tasks

    a_reconciled, b_waiting = asyncio.Event(), asyncio.Event()

    async def run_a() -> uuid.UUID:
        async with tenant_uow(world.tenant("acme")) as uow:
            await reconcile_tasks(uow, a)
            a_reconciled.set()
            await b_waiting.wait()
            return await a_opens(uow)

    async def run_b() -> uuid.UUID:
        await a_reconciled.wait()
        async with tenant_uow(world.tenant("acme")) as uow:
            await reconcile_tasks(uow, b)
            return await b_opens(uow)

    first, second = asyncio.create_task(run_a()), asyncio.create_task(run_b())
    await a_reconciled.wait()
    await _wait_for_lock_wait(world)
    b_waiting.set()
    return list(await asyncio.gather(first, second))


async def test_two_listings_of_one_product_ask_one_image_question_without_a_deadlock(
    gen_world: GenWorld,
) -> None:
    made = await generated(gen_world)
    product = _product(gen_world, made)
    image = f.image(gen_world.conn, gen_world.tenant("acme"), product, slot="main")
    task = await _open_image(gen_world, product, image)
    amazon = SimpleNamespace(id=made.listing_id, product_id=product)
    _linked(gen_world, amazon, task, "amazon")  # A's open vision row links the task
    flipkart = _sibling(gen_world, product)

    async def opens(uow: Any) -> uuid.UUID:
        return await uow.human_tasks.open_image_task(  # type: ignore[no-any-return]
            product, image, "Upload a new main image.", "The image failed its checks."
        )

    assert await _step4_race(gen_world, amazon, flipkart, opens, opens) == [task, task]
    assert gen_world.count("human_tasks", "image_id = %s", [image]) == 1


async def test_crossed_fact_questions_of_one_product_without_a_deadlock(
    gen_world: GenWorld,
) -> None:
    made = await generated(gen_world)
    product = _product(gen_world, made)
    fiber = await _open_fact(gen_world, product)
    country = await _open_fact(gen_world, product, target=COUNTRY)
    amazon = SimpleNamespace(id=made.listing_id, product_id=product)
    flipkart = _sibling(gen_world, product)
    _linked(gen_world, amazon, fiber, "amazon")  # A links fiber and needs country
    _linked(gen_world, flipkart, country, "flipkart")  # B links country and needs fiber

    def opens(target: str) -> Callable[[Any], Awaitable[uuid.UUID]]:
        async def go(uow: Any) -> uuid.UUID:
            return await uow.human_tasks.open_fact_task(  # type: ignore[no-any-return]
                product, target, QUESTION, REASON, dict(ANSWER)
            )

        return go

    got = await _step4_race(gen_world, amazon, flipkart, opens(COUNTRY), opens(TARGET))
    assert got == [country, fiber]
    assert _live_tasks(gen_world, product) == 2


async def test_a_photo_question_and_an_issue_question_on_one_fact_without_a_deadlock(
    gen_world: GenWorld,
) -> None:
    """SI3 and SI4 together: A's persist holds the product row (the photo step) and has an
    uncommitted photo question on the fact, while B's step 4 asks the same fact. B queues on
    the product row before any step-4 lock, so A's own step 4 still runs, and B then reuses
    A's question instead of a 40P01."""
    from app.repositories.uow import tenant_uow
    from app.services.issue_questions import reconcile_tasks

    made = await generated(gen_world)
    product = _product(gen_world, made)
    amazon = SimpleNamespace(id=made.listing_id, product_id=product)
    flipkart = _sibling(gen_world, product)
    a_asked, b_waiting = asyncio.Event(), asyncio.Event()

    async def run_a() -> uuid.UUID:
        async with tenant_uow(gen_world.tenant("acme")) as uow:
            await uow.products.get_for_update(product)  # SI3 apply_photo_decision
            assert await uow.human_tasks.open_photo_fact_task(
                product_id=product,
                kind="missing_fact",
                target_path=TARGET,
                question=QUESTION,
                reason=REASON,
                expected_answer=dict(ANSWER),
                suggestions=[],
                evidence=[],
            )
            a_asked.set()
            await b_waiting.wait()
            await reconcile_tasks(uow, amazon)
            return await uow.human_tasks.open_fact_task(
                product, TARGET, QUESTION, REASON, dict(ANSWER)
            )

    async def run_b() -> uuid.UUID:
        await a_asked.wait()
        async with tenant_uow(gen_world.tenant("acme")) as uow:
            await reconcile_tasks(uow, flipkart)
            return await uow.human_tasks.open_fact_task(
                product, TARGET, QUESTION, REASON, dict(ANSWER)
            )

    first, second = asyncio.create_task(run_a()), asyncio.create_task(run_b())
    await a_asked.wait()
    await _wait_for_lock_wait(gen_world)
    b_waiting.set()
    a_task, b_task = await asyncio.gather(first, second)
    assert a_task == b_task
    assert _live_tasks(gen_world, product) == 1


# ---- tenants ----


async def test_another_tenant_cannot_cancel_or_reuse(gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    product = _product(gen_world, made)
    task = await _open_fact(gen_world, product)
    _issue(gen_world, made, task, status="resolved")
    assert await _cancel(gen_world, task, "bharat") is False
    assert _task(gen_world, task) == ("open", None)

    async def reads(uow: Any) -> Any:
        return await uow.listing_issues.live_task_ids(made.listing_id)

    assert await _uow(gen_world, reads, "bharat") == []
