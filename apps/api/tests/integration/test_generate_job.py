"""S18 T16: the `listing_generate` body against the stub pipeline, as app_rw."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world, make_generating

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]


async def _run(world: GenWorld, instructions: str | None = None, sku: str = "KURTA-002") -> Any:
    from app.workers.tasks import register_bodies
    from app.workers.wrapper import run_job

    accepted = await make_generating(world, sku=sku, instructions=instructions)
    register_bodies()
    await run_job(str(accepted.job_id))
    return accepted


def _job(world: GenWorld, job_id: uuid.UUID) -> dict[str, Any]:
    cur = world.conn.execute("SELECT * FROM job_runs WHERE id = %s", [job_id])
    names = [d.name for d in cur.description or ()]
    row = cur.fetchone()
    assert row is not None
    return dict(zip(names, row, strict=True))


async def test_generate_happy_path(gen_world: GenWorld) -> None:
    accepted = await _run(gen_world)
    listing = gen_world.listing(accepted.listing_id)
    job = _job(gen_world, accepted.job_id)
    assert listing["status"] == "validated"
    assert listing["current_version_id"] is not None
    assert job["status"] == "succeeded"
    assert gen_world.count("listing_versions", "listing_id = %s", [accepted.listing_id]) == 1
    assert gen_world.count("validation_runs", "listing_id = %s", [accepted.listing_id]) >= 1
    assert gen_world.count("agent_runs", "job_id = %s", [accepted.job_id]) >= 1


async def test_generate_blocked_after_3(gen_world: GenWorld) -> None:
    accepted = await _run(gen_world, "stub:blocked")
    listing = gen_world.listing(accepted.listing_id)
    job = _job(gen_world, accepted.job_id)
    version = gen_world.conn.execute(
        "SELECT attempts_used FROM listing_versions WHERE listing_id = %s",
        [accepted.listing_id],
    ).fetchone()
    assert listing["status"] == "blocked"
    assert listing["status_reason"] == "corrections_exhausted"
    assert version is not None and version[0] == 3
    assert job["status"] == "succeeded"
    assert job["error_report_json"] is not None


async def test_generate_failed(gen_world: GenWorld) -> None:
    accepted = await _run(gen_world, "stub:failed")
    listing = gen_world.listing(accepted.listing_id)
    job = _job(gen_world, accepted.job_id)
    assert gen_world.count("listing_versions", "job_id = %s", [accepted.job_id]) == 0
    assert job["status"] == "failed"
    assert job["error_code"] == "GENERATION_FAILED"
    assert listing["status"] == "failed"
    assert listing["status_reason"] == "generation_failed"
