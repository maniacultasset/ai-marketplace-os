"""The per-tenant cap on live (`queued`/`running`) `listing_generate` jobs at enqueue
(`GENERATION_LIVE_JOBS_PER_TENANT`): past it, generate and regenerate are 429 `RATE_LIMITED` with a
`Retry-After`, nothing is written and nothing is enqueued; another tenant's jobs and finished
jobs do not count. Without it one brand could queue hundreds of jobs on the shared queue and
starve every other brand's generations, imports, image checks and brand embeds."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world
from .seam_support import running_job, terminal_job

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]
GENERATE = "/api/v1/listings/generate"


@pytest.fixture
def cap_two(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from app.core.config import get_settings

    monkeypatch.setenv("GENERATION_LIVE_JOBS_PER_TENANT", "2")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _jobs(world: GenWorld, tenant: uuid.UUID) -> int:
    return world.count("job_runs", "tenant_id = %s AND kind = 'listing_generate'", [tenant])


async def _generate(api: httpx.AsyncClient, world: GenWorld, sku: str = "KURTA-002") -> Any:
    body = {"product_id": str(world.product("acme", sku)), "marketplace": "amazon"}
    return await api.post(GENERATE, headers=world.headers("operator@acme.test"), json=body)


async def test_generate_refused_at_live_cap(
    api: httpx.AsyncClient, gen_world: GenWorld, cap_two: None
) -> None:
    acme = gen_world.tenant("acme")
    running_job(gen_world.conn, acme)
    running_job(gen_world.conn, acme, status="queued", started_at=None, lease_expires_at=None)
    listings = gen_world.count("listings", "tenant_id = %s", [acme])
    jobs = _jobs(gen_world, acme)
    response = await _generate(api, gen_world)
    assert response.status_code == 429, response.text
    assert response.json()["error"]["type"] == "RATE_LIMITED"
    assert int(response.headers["retry-after"]) >= 1
    assert gen_world.count("listings", "tenant_id = %s", [acme]) == listings
    assert _jobs(gen_world, acme) == jobs


async def test_generate_cap_ignores_finished_and_other_tenants(
    api: httpx.AsyncClient, gen_world: GenWorld, cap_two: None
) -> None:
    acme, bharat = gen_world.tenant("acme"), gen_world.tenant("bharat")
    running_job(gen_world.conn, acme)
    terminal_job(gen_world.conn, acme)
    running_job(gen_world.conn, bharat)
    running_job(gen_world.conn, bharat)
    response = await _generate(api, gen_world)
    assert response.status_code == 202, response.text


async def test_regenerate_refused_at_live_cap(
    api: httpx.AsyncClient, gen_world: GenWorld, cap_two: None
) -> None:
    from app.workers.tasks import register_bodies
    from app.workers.wrapper import run_job

    acme = gen_world.tenant("acme")
    created = await _generate(api, gen_world)
    assert created.status_code == 202, created.text
    register_bodies()
    await run_job(created.json()["job_id"])
    running_job(gen_world.conn, acme)
    running_job(gen_world.conn, acme)
    jobs = _jobs(gen_world, acme)
    response = await api.post(
        f"/api/v1/listings/{created.json()['listing_id']}/regenerate",
        headers=gen_world.headers("reviewer@acme.test"),
        json={"instructions": "shorter title"},
    )
    assert response.status_code == 429, response.text
    assert response.json()["error"]["type"] == "RATE_LIMITED"
    assert _jobs(gen_world, acme) == jobs
