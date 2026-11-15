"""The per-tenant cap on live `listing_generate` jobs (`GENERATION_LIVE_JOBS_PER_TENANT`) and, since
B19, the backlog past it: a generate or regenerate past the cap is still 202, its job written
`queued` with no queue entry (`waiting_for_slot`), and the tenant's oldest waiting job takes each
slot a finished generation frees (the terminal hook, the worker's post-commit pass, the reaper's
sweep), under the tenant's generate lock. The tenant never holds more than the cap on the shared
queue, so one brand's bulk send cannot starve the others; another tenant's jobs never count."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest

from . import factories as f
from .conftest import LOCAL_HOSTS
from .generation_support import CATEGORY, GenWorld, gen_world
from .jobs_support import job_row
from .seam_support import running_job, terminal_job

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]
GENERATE = "/api/v1/listings/generate"
OPERATOR = "operator@acme.test"
GENERATE_JOBS = "tenant_id = %s AND kind = 'listing_generate'"
HOLDS_SLOT = (
    f"{GENERATE_JOBS} AND (status = 'running' OR (status = 'queued' AND queue_job_id IS NOT NULL))"
)
WAITING = f"{GENERATE_JOBS} AND status = 'queued' AND queue_job_id IS NULL"


def _cap(monkeypatch: pytest.MonkeyPatch, value: int) -> Iterator[None]:
    from app.core.config import get_settings

    monkeypatch.setenv("GENERATION_LIVE_JOBS_PER_TENANT", str(value))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def cap_one(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    yield from _cap(monkeypatch, 1)


@pytest.fixture
def cap_two(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    yield from _cap(monkeypatch, 2)


@pytest.fixture
def cap_three(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    yield from _cap(monkeypatch, 3)


@pytest.fixture
def cap_ten(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    yield from _cap(monkeypatch, 10)


def _products(world: GenWorld, n: int, slug: str = "acme") -> list[uuid.UUID]:
    """`n` standalone products in a category with a current Amazon template."""
    return [f.product(world.conn, world.tenant(slug), category=CATEGORY) for _ in range(n)]


async def _accept(
    world: GenWorld, product_id: uuid.UUID, *, email: str = OPERATOR, key: str | None = None
) -> dict[str, Any]:
    """`accept_generate` as the route calls it (the idempotent 202 body)."""
    from app.services.generation import accept_generate
    from mkos_schemas.enums import Marketplace
    from mkos_schemas.jobs import GenerateListingRequest

    body = GenerateListingRequest(product_id=product_id, marketplace=Marketplace.AMAZON)
    return await accept_generate(world.principal(email), key or str(uuid.uuid4()), GENERATE, body)


def _count(world: GenWorld, where: str, tenant: uuid.UUID) -> int:
    return world.count("job_runs", where, [tenant])


def _job_id(body: dict[str, Any]) -> uuid.UUID:
    return uuid.UUID(body["job_id"])


def _waiting(world: GenWorld, job_id: uuid.UUID) -> bool:
    row = job_row(world.conn, job_id)
    return row["status"] == "queued" and row["queue_job_id"] is None


async def _finish(job_id: uuid.UUID) -> None:
    from app.workers.tasks import register_bodies
    from app.workers.wrapper import run_job

    register_bodies()
    await run_job(str(job_id))


async def test_bulk_send_past_cap_is_accepted_and_waits(gen_world: GenWorld, cap_ten: None) -> None:
    """The prod case: 34 drafts sent 3 at a time (the web's `generateMany`) with the cap at 10."""
    acme = gen_world.tenant("acme")
    products = _products(gen_world, 34)
    gate = asyncio.Semaphore(3)

    async def send(product_id: uuid.UUID) -> dict[str, Any]:
        async with gate:
            return await _accept(gen_world, product_id)

    bodies = await asyncio.gather(*(send(p) for p in products))
    assert len({b["job_id"] for b in bodies}) == 34
    assert all(b["status"] == "queued" for b in bodies)
    assert sum(b["waiting_for_slot"] for b in bodies) == 24
    assert _count(gen_world, HOLDS_SLOT, acme) == 10
    assert _count(gen_world, WAITING, acme) == 24
    for body in bodies:  # the 202 says what the row is
        assert body["waiting_for_slot"] is _waiting(gen_world, _job_id(body))
    entries = gen_world.scalar(
        "SELECT count(*) FROM procrastinate_jobs WHERE task_name = 'listing_generate'"
    )
    assert entries == 10
    statuses = {gen_world.listing(uuid.UUID(b["listing_id"]))["status"] for b in bodies}
    assert statuses == {"generating"}


async def test_finish_promotes_exactly_the_oldest_waiting(
    api: httpx.AsyncClient, gen_world: GenWorld, cap_two: None
) -> None:
    acme = gen_world.tenant("acme")
    first, second, third, fourth = [await _accept(gen_world, p) for p in _products(gen_world, 4)]
    assert [b["waiting_for_slot"] for b in (first, second, third, fourth)] == [
        False,
        False,
        True,
        True,
    ]
    headers = gen_world.headers(OPERATOR)
    read = await api.get(f"/api/v1/jobs/{third['job_id']}", headers=headers)
    assert read.status_code == 200, read.text
    assert (read.json()["status"], read.json()["waiting_for_slot"]) == ("queued", True)

    await _finish(_job_id(first))
    assert job_row(gen_world.conn, _job_id(first))["status"] not in ("queued", "running")
    assert not _waiting(gen_world, _job_id(third))  # the older waiting one took the slot
    assert _waiting(gen_world, _job_id(fourth))
    assert _count(gen_world, HOLDS_SLOT, acme) == 2
    read = await api.get(f"/api/v1/jobs/{third['job_id']}", headers=headers)
    assert (read.json()["status"], read.json()["waiting_for_slot"]) == ("queued", False)


async def test_cancel_frees_the_slot_and_a_waiting_job_cancels(
    gen_world: GenWorld, cap_one: None
) -> None:
    from app.services import jobs

    acme = gen_world.tenant("acme")
    live, waiting, last = [await _accept(gen_world, p) for p in _products(gen_world, 3)]
    principal = gen_world.principal(OPERATOR)
    # a waiting job is cancelled like any queued one, and frees no slot
    cancelled = await jobs.cancel(
        principal, _job_id(last), str(uuid.uuid4()), f"/api/v1/jobs/{last['job_id']}/cancel"
    )
    assert (cancelled.status, cancelled.waiting_for_slot) == ("cancelled", False)
    listing = gen_world.listing(uuid.UUID(last["listing_id"]))
    assert (listing["status"], listing["status_reason"]) == ("failed", "generation_failed")
    assert _waiting(gen_world, _job_id(waiting))
    # cancelling the live one promotes the waiting one in the same transaction (the hook)
    await jobs.cancel(
        principal, _job_id(live), str(uuid.uuid4()), f"/api/v1/jobs/{live['job_id']}/cancel"
    )
    assert not _waiting(gen_world, _job_id(waiting))
    assert _count(gen_world, HOLDS_SLOT, acme) == 1
    assert _count(gen_world, WAITING, acme) == 0


async def test_concurrent_finishes_fill_but_never_pass_the_cap(
    gen_world: GenWorld, cap_three: None
) -> None:
    acme = gen_world.tenant("acme")
    bodies = [await _accept(gen_world, p) for p in _products(gen_world, 9)]
    live = [_job_id(b) for b in bodies if not b["waiting_for_slot"]]
    assert len(live) == 3
    await asyncio.gather(*(_finish(job_id) for job_id in live))
    assert _count(gen_world, HOLDS_SLOT, acme) == 3
    assert _count(gen_world, WAITING, acme) == 3
    promoted = [_job_id(b) for b in bodies[3:6]]  # FIFO: the three oldest waiting ones
    assert not any(_waiting(gen_world, job_id) for job_id in promoted)
    # a second pass (the reaper's) finds no free slot
    from app.services.generation_backlog import promote_tenant

    assert await promote_tenant(acme) == []
    assert _count(gen_world, HOLDS_SLOT, acme) == 3


async def test_replay_of_a_waiting_request_returns_the_same_job(
    gen_world: GenWorld, cap_one: None
) -> None:
    acme = gen_world.tenant("acme")
    first, product = _products(gen_world, 2)
    await _accept(gen_world, first)
    key = str(uuid.uuid4())
    body = await _accept(gen_world, product, key=key)
    assert body["waiting_for_slot"] is True
    jobs = _count(gen_world, GENERATE_JOBS, acme)
    replay = await _accept(gen_world, product, key=key)
    assert replay == body
    assert _count(gen_world, GENERATE_JOBS, acme) == jobs
    assert _waiting(gen_world, _job_id(body))


async def test_other_tenants_never_count_or_promote(gen_world: GenWorld, cap_one: None) -> None:
    acme, bharat = gen_world.tenant("acme"), gen_world.tenant("bharat")
    a_live, a_waiting = [await _accept(gen_world, p) for p in _products(gen_world, 2)]
    assert a_waiting["waiting_for_slot"] is True
    [b_product] = _products(gen_world, 1, "bharat")
    b_live = await _accept(gen_world, b_product, email="operator@bharat.test")
    assert b_live["waiting_for_slot"] is False  # acme's backlog is not bharat's
    await _finish(_job_id(b_live))
    assert _waiting(gen_world, _job_id(a_waiting))  # bharat's end frees no acme slot
    assert _count(gen_world, HOLDS_SLOT, acme) == 1
    assert _count(gen_world, HOLDS_SLOT, bharat) == 0
    await _finish(_job_id(a_live))
    assert not _waiting(gen_world, _job_id(a_waiting))


async def test_reaper_sweep_promotes_a_stranded_waiting_job(
    gen_world: GenWorld, cap_one: None
) -> None:
    from app.workers.periodic import reap_once

    acme = gen_world.tenant("acme")
    live, waiting = [await _accept(gen_world, p) for p in _products(gen_world, 2)]
    # the live job ends with no hook and no post-commit pass (a crash between them)
    gen_world.conn.execute(
        "UPDATE job_runs SET status = 'running', attempt = 1, started_at = now(), "
        "lease_expires_at = now() + interval '5 minutes' WHERE id = %s",
        [_job_id(live)],
    )
    gen_world.conn.execute(
        "UPDATE job_runs SET status = 'succeeded', lease_expires_at = NULL, finished_at = now() "
        "WHERE id = %s",
        [_job_id(live)],
    )
    assert _waiting(gen_world, _job_id(waiting))
    result = await reap_once()
    assert result.promoted == [_job_id(waiting)]
    assert not _waiting(gen_world, _job_id(waiting))
    assert _count(gen_world, HOLDS_SLOT, acme) == 1


async def test_cap_ignores_finished_jobs_and_other_tenants(
    gen_world: GenWorld, cap_two: None
) -> None:
    acme, bharat = gen_world.tenant("acme"), gen_world.tenant("bharat")
    running_job(gen_world.conn, acme)
    terminal_job(gen_world.conn, acme)
    running_job(gen_world.conn, bharat)
    running_job(gen_world.conn, bharat)
    [product] = _products(gen_world, 1)
    body = await _accept(gen_world, product)
    assert body["waiting_for_slot"] is False


async def test_promoted_job_past_the_budget_fails_with_the_seller_reason(
    gen_world: GenWorld, cap_one: None
) -> None:
    """A waiting job is never dropped: promoted after the day's budget ran out, its claim refuses
    it like any queued job (`BUDGET_EXCEEDED`), and its listing is `failed`."""
    acme = gen_world.tenant("acme")
    live, waiting = [await _accept(gen_world, p) for p in _products(gen_world, 2)]
    f.agent_run(gen_world.conn, acme, cost_usd=Decimal("2.500000"), started_at=datetime.now(UTC))
    await _finish(_job_id(live))
    assert not _waiting(gen_world, _job_id(waiting))
    await _finish(_job_id(waiting))
    job = job_row(gen_world.conn, _job_id(waiting))
    assert (job["status"], job["error_code"]) == ("failed", "BUDGET_EXCEEDED")
    assert job["error_report_json"]["errors"][0]["message"] == "The daily AI budget is used up."
    listing = gen_world.listing(uuid.UUID(waiting["listing_id"]))
    assert (listing["status"], listing["status_reason"]) == ("failed", "generation_failed")


async def test_regenerate_past_cap_waits(
    api: httpx.AsyncClient, gen_world: GenWorld, cap_two: None
) -> None:
    acme = gen_world.tenant("acme")
    body = {"product_id": str(gen_world.product("acme", "KURTA-002")), "marketplace": "amazon"}
    created = await api.post(GENERATE, headers=gen_world.headers(OPERATOR), json=body)
    assert created.status_code == 202, created.text
    assert created.json()["waiting_for_slot"] is False
    await _finish(uuid.UUID(created.json()["job_id"]))
    running_job(gen_world.conn, acme)
    running_job(gen_world.conn, acme)
    response = await api.post(
        f"/api/v1/listings/{created.json()['listing_id']}/regenerate",
        headers=gen_world.headers("reviewer@acme.test"),
        json={"instructions": "shorter title"},
    )
    assert response.status_code == 202, response.text
    assert (response.json()["status"], response.json()["waiting_for_slot"]) == ("queued", True)
    assert _waiting(gen_world, uuid.UUID(response.json()["job_id"]))
    listing = gen_world.listing(uuid.UUID(created.json()["listing_id"]))
    assert listing["status"] == "generating"
