"""S13a round-1 tester attacks (AC3, AC5, AC6, AC8, AC9): duplicate deliveries at once, a cancel
during retry backoff, a concurrent staff requeue, Idempotency-Key bounds and a cross-tenant cancel
that reuses the other tenant's key. Real stack: the API as app_rw, the wrapper, Postgres."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from app import cli
from app.db.session import unit_of_work
from app.repositories.jobs import EnqueueSpec, JobRunRepository
from app.workers import wrapper
from app.workers.registry import JobContext, Outcome
from mkos_schemas.enums import JobKind

from .auth_support import PREFIX
from .conftest import LOCAL_HOSTS
from .jobs_support import (
    JobWorld,
    as_member,
    count,
    insert_job,
    job_row,
    jobs_db,
    override_body,
    queue_row,
)

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["jobs_db", "override_body"]
KIND = JobKind.BULK_IMPORT


async def _queued(jw: JobWorld, slug: str = "acme") -> tuple[uuid.UUID, str]:
    tenant = jw.tenant(slug)
    async with unit_of_work(tenant) as session:
        done = await JobRunRepository(session, tenant).enqueue(
            EnqueueSpec(KIND, "import", uuid.uuid4(), jw.owner(slug))
        )
    assert done.job.queue_job_id is not None
    return done.job.id, done.job.queue_job_id


_DEAD: dict[str, Any] = {
    "status": "dead_letter",
    "attempt": 3,
    "error_code": "RETRY_EXHAUSTED",
    "error_report_json": {"status": "failed", "error_code": "RETRY_EXHAUSTED"},
    "finished_at": datetime.now(UTC),
}


def _cancel(job: uuid.UUID) -> str:
    return f"{PREFIX}/jobs/{job}/cancel"


async def test_attack_eight_concurrent_deliveries_run_body_once(
    jobs_db: JobWorld, override_body: Any
) -> None:
    runs: list[int] = []

    async def body(ctx: JobContext) -> Outcome:
        runs.append(ctx.attempt)
        await asyncio.sleep(0.2)
        return Outcome("succeeded")

    override_body(KIND, body)
    job, _ = await _queued(jobs_db)
    await asyncio.gather(*(wrapper.run_job(str(job)) for _ in range(8)))
    assert runs == [1]
    row = job_row(jobs_db.conn, job)
    assert (row["status"], row["attempt"]) == ("succeeded", 1)


async def test_attack_cancel_during_retry_backoff_never_runs_again(
    jobs_db: JobWorld, api: httpx.AsyncClient, override_body: Any
) -> None:
    runs: list[int] = []

    async def body(ctx: JobContext) -> Outcome:
        runs.append(ctx.attempt)
        return Outcome("succeeded")

    override_body(KIND, body)
    job, qid = await _queued(jobs_db)
    # what `release_retry` + `RetryLater` leave: the row queued with attempt 1 kept, the entry
    # back to `todo` with a later `scheduled_at`
    jobs_db.conn.execute("UPDATE job_runs SET attempt = 1 WHERE id = %s", [job])
    jobs_db.conn.execute(
        "UPDATE procrastinate_jobs SET attempts = 1, scheduled_at = now() + interval '1 hour' "
        "WHERE id = %s",
        [int(qid)],
    )
    headers = {**as_member(jobs_db, "operator@acme.test"), "Idempotency-Key": "backoff-cancel"}
    response = await api.post(_cancel(job), headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["attempt"] == 1
    assert queue_row(jobs_db.conn, qid)["status"] == "cancelled"
    await wrapper.run_job(str(job))  # a stale delivery anyway
    assert runs == []
    assert job_row(jobs_db.conn, job)["status"] == "cancelled"


async def test_attack_two_concurrent_requeues_one_child(jobs_db: JobWorld) -> None:
    old = insert_job(
        jobs_db.conn,
        jobs_db.tenant("acme"),
        **_DEAD,
    )
    codes = await asyncio.gather(
        cli.run_requeue(old, "ops@staff.test", "a"), cli.run_requeue(old, "ops@staff.test", "b")
    )
    assert sorted(codes) == [0, 4]
    assert count(jobs_db.conn, "SELECT count(*) FROM job_runs WHERE parent_job_id = %s", [old]) == 1
    assert count(jobs_db.conn, "SELECT count(*) FROM audit_log WHERE action = 'job.requeue'") == 1


async def test_attack_requeue_of_a_child_chain_keeps_one_child_each(jobs_db: JobWorld) -> None:
    """A requeued job that dead-letters again can itself be requeued; each row has one child."""
    old = insert_job(
        jobs_db.conn,
        jobs_db.tenant("acme"),
        **_DEAD,
    )
    assert await cli.run_requeue(old, "ops@staff.test", "r1") == 0
    child = jobs_db.conn.execute(
        "SELECT id FROM job_runs WHERE parent_job_id = %s", [old]
    ).fetchone()
    assert child is not None
    jobs_db.conn.execute(
        "UPDATE job_runs SET status = 'running', attempt = 3, started_at = now(), "
        "lease_expires_at = now() WHERE id = %s",
        [child[0]],
    )
    jobs_db.conn.execute(
        "UPDATE job_runs SET status = 'dead_letter', attempt = 3, finished_at = now(), "
        "error_code = 'RETRY_EXHAUSTED', error_report_json = '{}'::jsonb WHERE id = %s",
        [child[0]],
    )
    assert await cli.run_requeue(child[0], "ops@staff.test", "r2") == 0
    assert await cli.run_requeue(old, "ops@staff.test", "r3") == 4


@pytest.mark.parametrize(
    ("key", "status"),
    [("k" * 255, 200), ("k" * 256, 422), ("has space", 422), ("tab\tkey", 422), ("", 422)],
)
async def test_attack_idempotency_key_bounds(
    jobs_db: JobWorld, api: httpx.AsyncClient, key: str, status: int
) -> None:
    job, _ = await _queued(jobs_db)
    headers = {**as_member(jobs_db, "owner@acme.test"), "Idempotency-Key": key}
    response = await api.post(_cancel(job), headers=headers)
    assert response.status_code == status, response.text
    expected = "cancelled" if status == 200 else "queued"
    assert job_row(jobs_db.conn, job)["status"] == expected
    stored = count(jobs_db.conn, "SELECT count(*) FROM idempotency_keys")
    assert stored == (1 if status == 200 else 0)


async def test_attack_idempotency_key_non_ascii_refused(
    jobs_db: JobWorld, api: httpx.AsyncClient
) -> None:
    job, _ = await _queued(jobs_db)
    headers = {**as_member(jobs_db, "owner@acme.test"), "Idempotency-Key": "clé".encode("latin-1")}
    response = await api.post(_cancel(job), headers=headers)  # type: ignore[arg-type]
    assert response.status_code == 422
    assert job_row(jobs_db.conn, job)["status"] == "queued"


async def test_attack_same_key_in_two_tenants_never_replays_across(
    jobs_db: JobWorld, api: httpx.AsyncClient
) -> None:
    """bharat used key K to cancel its job; acme's owner sending K for bharat's job is 404 (not a
    replay of bharat's stored response) and acme's own use of K is fresh."""
    theirs, _ = await _queued(jobs_db, "bharat")
    bharat = {**as_member(jobs_db, "owner@bharat.test"), "Idempotency-Key": "shared-key"}
    first = await api.post(_cancel(theirs), headers=bharat)
    assert first.status_code == 200
    acme = {**as_member(jobs_db, "owner@acme.test"), "Idempotency-Key": "shared-key"}
    probe = await api.post(_cancel(theirs), headers=acme)
    assert probe.status_code == 404
    assert str(theirs) not in probe.text
    mine, _ = await _queued(jobs_db, "acme")
    assert (await api.post(_cancel(mine), headers=acme)).status_code == 200
    assert job_row(jobs_db.conn, mine)["status"] == "cancelled"


async def test_attack_jobs_get_cross_tenant_body_equals_unknown(
    jobs_db: JobWorld, api: httpx.AsyncClient
) -> None:
    theirs = insert_job(jobs_db.conn, jobs_db.tenant("bharat"))
    headers = as_member(jobs_db, "reviewer@acme.test")
    other = await api.get(f"{PREFIX}/jobs/{theirs}", headers=headers)
    unknown = await api.get(f"{PREFIX}/jobs/{uuid.uuid4()}", headers=headers)
    assert (other.status_code, unknown.status_code) == (404, 404)
    assert _without_ids(other.json()) == _without_ids(unknown.json())
    assert str(theirs) not in other.text


def _without_ids(value: Any) -> Any:
    ids = {"request_id", "correlation_id", "trace_id", "span_id"}
    if isinstance(value, dict):
        return {k: _without_ids(v) for k, v in value.items() if k not in ids}
    if isinstance(value, list):
        return [_without_ids(v) for v in value]
    return value
