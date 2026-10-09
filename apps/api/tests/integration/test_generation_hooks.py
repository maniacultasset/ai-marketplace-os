"""S18 T17: the `listing_generate` terminal and requeue hooks (timeout, reaper, cancel, requeue)."""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from decimal import Decimal
from typing import Any

import pytest

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world, make_generating
from .jobs_support import job_row

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]


def _expire(world: GenWorld, job_id: uuid.UUID) -> None:
    """The worker died mid-run: the row is `running` on its last attempt with a lapsed lease."""
    world.conn.execute(
        "UPDATE job_runs SET status = 'running', attempt = max_attempts, started_at = now(), "
        "lease_expires_at = now() - interval '1 minute' WHERE id = %s",
        [job_id],
    )


def _listing_state(world: GenWorld, listing_id: uuid.UUID) -> tuple[str, str | None]:
    row = world.listing(listing_id)
    return row["status"], row["status_reason"]


async def test_generation_reaper(gen_world: GenWorld) -> None:
    from app.workers.periodic import reap_once

    accepted = await make_generating(gen_world)
    _expire(gen_world, accepted.job_id)
    await reap_once()
    job = job_row(gen_world.conn, accepted.job_id)
    assert (job["status"], job["error_code"]) == ("dead_letter", "JOB_TIMEOUT")
    assert _listing_state(gen_world, accepted.listing_id) == ("failed", "generation_failed")
    assert gen_world.count("listing_versions", "job_id = %s", [accepted.job_id]) == 0


async def test_generation_timeout(gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.workers import generation, wrapper
    from app.workers.policy import POLICIES
    from app.workers.tasks import register_bodies

    async def slow(*_: Any, **__: Any) -> Any:
        await asyncio.sleep(30)

    def short(kind: Any) -> Any:
        return dataclasses.replace(POLICIES[kind], timeout_s=1)

    monkeypatch.setattr(generation, "run_listing_pipeline", slow)
    monkeypatch.setattr(wrapper, "policy_for", short)
    accepted = await make_generating(gen_world)
    register_bodies()
    await wrapper.run_job(str(accepted.job_id))
    job = job_row(gen_world.conn, accepted.job_id)
    assert (job["status"], job["error_code"]) == ("failed", "JOB_TIMEOUT")
    assert job["cost_reserved_usd"] == Decimal("0.10")  # a timeout stays counted (DB:998)
    assert _listing_state(gen_world, accepted.listing_id) == ("failed", "generation_failed")


async def test_generation_cancel_hook(gen_world: GenWorld) -> None:
    from app.services import jobs

    accepted = await make_generating(gen_world)
    principal = gen_world.principal("operator@acme.test")
    path = f"/api/v1/jobs/{accepted.job_id}/cancel"
    await jobs.cancel(principal, accepted.job_id, str(uuid.uuid4()), path)
    assert job_row(gen_world.conn, accepted.job_id)["status"] == "cancelled"
    assert _listing_state(gen_world, accepted.listing_id) == ("failed", "generation_failed")


async def test_generation_non_running_tenant_cancel_moves(gen_world: GenWorld) -> None:
    from app.repositories.uow import tenant_uow
    from app.services.job_hooks import run_terminal_hook

    accepted = await make_generating(gen_world)
    acme = gen_world.tenant("acme")
    gen_world.conn.execute("UPDATE tenants SET status = 'suspended' WHERE id = %s", [acme])
    try:
        async with tenant_uow(acme) as uow:
            job = await uow.job_runs.cancel_non_running(accepted.job_id)
            assert job is not None
            await run_terminal_hook(uow.session, job, None)
    finally:
        gen_world.conn.execute("UPDATE tenants SET status = 'active' WHERE id = %s", [acme])
    assert job_row(gen_world.conn, accepted.job_id)["status"] == "cancelled"
    assert _listing_state(gen_world, accepted.listing_id) == ("failed", "generation_failed")


async def _call_hook(world: GenWorld, job_id: uuid.UUID, status: str) -> None:
    """The hook on a detached copy of the job with `status`, `outcome=None`, committed."""
    from app.repositories.uow import tenant_uow
    from app.services.generation_hooks import on_generate_terminal

    async with tenant_uow(world.tenant("acme")) as uow:
        job = await uow.job_runs.get_by_id(job_id)
        assert job is not None
        uow.session.expunge(job)  # the hook must not flush the copy
        job.status = status
        await on_generate_terminal(uow.session, job, None)


@pytest.mark.parametrize("status", ["failed", "dead_letter", "cancelled"])
async def test_generation_hook_none_outcome_branches_on_status(
    gen_world: GenWorld, status: str
) -> None:
    accepted = await make_generating(gen_world)
    await _call_hook(gen_world, accepted.job_id, status)
    assert _listing_state(gen_world, accepted.listing_id) == ("failed", "generation_failed")


async def test_generation_hook_none_outcome_succeeded_noop(gen_world: GenWorld) -> None:
    accepted = await make_generating(gen_world)
    await _call_hook(gen_world, accepted.job_id, "succeeded")
    assert _listing_state(gen_world, accepted.listing_id)[0] == "generating"


async def test_generation_hook_after_success_noop(gen_world: GenWorld) -> None:
    from app.workers.tasks import register_bodies
    from app.workers.wrapper import run_job

    accepted = await make_generating(gen_world)
    register_bodies()
    await run_job(str(accepted.job_id))
    assert _listing_state(gen_world, accepted.listing_id)[0] == "validated"
    await _call_hook(gen_world, accepted.job_id, "failed")  # a late, stray hook call
    assert _listing_state(gen_world, accepted.listing_id)[0] == "validated"


async def test_generation_hook_fenced(gen_world: GenWorld) -> None:
    from app.repositories.uow import tenant_uow
    from app.services.generation import request_regeneration
    from app.workers.tasks import register_bodies
    from app.workers.wrapper import run_job

    first = await make_generating(gen_world, instructions="stub:failed")
    register_bodies()
    await run_job(str(first.job_id))
    assert _listing_state(gen_world, first.listing_id) == ("failed", "generation_failed")
    principal = gen_world.principal("operator@acme.test")
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        second = await request_regeneration(
            uow, principal, str(uuid.uuid4()), first.listing_id, None
        )
    assert _listing_state(gen_world, first.listing_id)[0] == "generating"
    await _call_hook(gen_world, first.job_id, "failed")  # the old job's hook, replayed
    assert _listing_state(gen_world, first.listing_id)[0] == "generating"
    assert job_row(gen_world.conn, second.job_id)["status"] == "queued"


async def test_requeue_generate_refused_exit_4(
    gen_world: GenWorld, capsys: pytest.CaptureFixture[str]
) -> None:
    from app import cli
    from app.workers.periodic import reap_once

    accepted = await make_generating(gen_world)
    _expire(gen_world, accepted.job_id)
    await reap_once()
    assert job_row(gen_world.conn, accepted.job_id)["status"] == "dead_letter"
    jobs_before = gen_world.count("job_runs", "listing_id = %s", [accepted.listing_id])
    assert await cli.run_requeue(accepted.job_id, "ops@staff.test", "retry") == 4
    assert "regenerate through the API" in capsys.readouterr().err
    assert gen_world.count("job_runs", "listing_id = %s", [accepted.listing_id]) == jobs_before
    assert _listing_state(gen_world, accepted.listing_id) == ("failed", "generation_failed")
