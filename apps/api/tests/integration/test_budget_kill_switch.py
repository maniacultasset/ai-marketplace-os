"""S18 T19: the daily AI budget end to end (DW8, AC 14; D10): the API pre-check, the claim-time
reservation against the tenant and the global cap, and the reservation zeroed on success."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest

from . import factories as f
from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world, make_generating, set_status
from .jobs_support import job_row

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]
GENERATE = "/api/v1/listings/generate"


def _spend(world: GenWorld, slug: str, usd: str) -> None:
    """Today's (UTC) model spend of `slug`, written by the admin login."""
    f.agent_run(world.conn, world.tenant(slug), cost_usd=Decimal(usd), started_at=datetime.now(UTC))


async def _run(job_id: uuid.UUID) -> None:
    from app.workers.tasks import register_bodies
    from app.workers.wrapper import run_job

    register_bodies()
    await run_job(str(job_id))


def _counts(world: GenWorld, slug: str) -> tuple[int, ...]:
    tenant = world.tenant(slug)
    tables = ("listings", "job_runs", "idempotency_keys", "review_decisions")
    rows = [world.count(table, "tenant_id = %s", [tenant]) for table in tables]
    return (*rows, world.count("procrastinate_jobs"))


async def test_budget_regenerate_429(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    listing_id = gen_world.listing_of(gen_world.product("acme", "KURTA-001"))
    assert listing_id is not None
    set_status(gen_world, listing_id, "validated")
    _spend(gen_world, "acme", "2.500000")
    before = _counts(gen_world, "acme")
    response = await api.post(
        f"/api/v1/listings/{listing_id}/regenerate",
        headers=gen_world.headers("operator@acme.test"),
        json={},
    )
    assert response.status_code == 429, response.text
    body = response.json()
    assert body["error"]["type"] == "BUDGET_EXCEEDED"
    assert 1 <= int(response.headers["retry-after"]) <= 86400
    assert "2.5" not in response.text  # the body states no spend figure
    assert _counts(gen_world, "acme") == before
    assert gen_world.listing(listing_id)["status"] == "validated"


async def test_budget_reservation_claim_over_cap_fails_job(gen_world: GenWorld) -> None:
    accepted = await make_generating(gen_world)  # accepted under the cap
    _spend(gen_world, "acme", "2.450000")  # the cap is reached before the claim
    await _run(accepted.job_id)
    job = job_row(gen_world.conn, accepted.job_id)
    assert (job["status"], job["error_code"]) == ("failed", "BUDGET_EXCEEDED")
    assert job["cost_reserved_usd"] == Decimal("0")
    listing = gen_world.listing(accepted.listing_id)
    assert (listing["status"], listing["status_reason"]) == ("failed", "generation_failed")
    assert gen_world.count("listing_versions", "job_id = %s", [accepted.job_id]) == 0
    assert gen_world.count("agent_runs", "job_id = %s", [accepted.job_id]) == 0


async def test_budget_reservation_zeroed_on_success(gen_world: GenWorld) -> None:
    accepted = await make_generating(gen_world)
    await _run(accepted.job_id)
    job = job_row(gen_world.conn, accepted.job_id)
    assert job["status"] == "succeeded"
    assert job["cost_reserved_usd"] == Decimal("0")
    assert gen_world.count("agent_runs", "job_id = %s", [accepted.job_id]) >= 1


async def test_budget_other_tenant_unaffected(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    _spend(gen_world, "acme", "2.500000")
    response = await api.post(
        GENERATE,
        headers=gen_world.headers("operator@bharat.test"),
        json={"product_id": str(gen_world.product("bharat", "KURTA-002")), "marketplace": "amazon"},
    )
    assert response.status_code == 202, response.text
    await _run(uuid.UUID(response.json()["job_id"]))
    assert job_row(gen_world.conn, uuid.UUID(response.json()["job_id"]))["status"] == "succeeded"


async def test_budget_global_cap_fails_at_claim(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    """The D10 departure: the platform cap is checked at claim only, so the API accepts."""
    _spend(gen_world, "bharat", "12.000000")  # the whole platform day, acme still under its cap
    response = await api.post(
        GENERATE,
        headers=gen_world.headers("operator@acme.test"),
        json={"product_id": str(gen_world.product("acme", "KURTA-002")), "marketplace": "amazon"},
    )
    assert response.status_code == 202, response.text
    body: dict[str, Any] = response.json()
    await _run(uuid.UUID(body["job_id"]))
    job = job_row(gen_world.conn, uuid.UUID(body["job_id"]))
    assert (job["status"], job["error_code"]) == ("failed", "BUDGET_EXCEEDED")
    listing = gen_world.listing(uuid.UUID(body["listing_id"]))
    assert (listing["status"], listing["status_reason"]) == ("failed", "generation_failed")
    assert listing["current_version_id"] is None


async def test_budget_actual_cost_above_reservation_blocks_next_claim(
    gen_world: GenWorld,
) -> None:
    """One run may spend past its 0.10 reservation (D10 residual risk); the next claim sees it."""
    _spend(gen_world, "acme", "2.350000")
    first = await make_generating(gen_world)
    await _run(first.job_id)
    assert job_row(gen_world.conn, first.job_id)["status"] == "succeeded"
    _spend(gen_world, "acme", "0.100000")  # the first run's spend landed above its reservation
    second = await make_generating(gen_world, sku="KURTA-001")
    await _run(second.job_id)
    job = job_row(gen_world.conn, second.job_id)
    assert (job["status"], job["error_code"]) == ("failed", "BUDGET_EXCEEDED")
