"""S13 A14 (AC8; PLAN D7 as Q2; BACKEND-ARCHITECTURE:1718): `POST /api/v1/jobs/{job_id}/cancel`
cancels a queued job and its queue entry under an Idempotency-Key, as app_rw; running or
finished is 409; the terminal hooks get `outcome=None` on every non-success path."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from app.db.queue import get_queue_app
from app.db.session import unit_of_work
from app.repositories.jobs import EnqueueSpec, JobRunRepository
from app.services import job_hooks
from app.workers import wrapper
from app.workers.errors import RetryableJobError
from app.workers.periodic import reap_once
from app.workers.registry import JobContext, Outcome
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import JobKind
from mkos_schemas.jobs import JobEnvelope

from .auth_support import PREFIX, add_tenant
from .conftest import LOCAL_HOSTS
from .jobs_support import (
    JobWorld,
    as_member,
    bad_token,
    count,
    eventually,
    insert_job,
    job_row,
    jobs_db,
    override_body,
    queue_row,
    running_worker,
)

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["jobs_db", "override_body"]
KIND = JobKind.BULK_IMPORT


def _url(job: uuid.UUID | str) -> str:
    return f"{PREFIX}/jobs/{job}/cancel"


def _headers(
    jw: JobWorld, email: str = "owner@acme.test", key: str | None = None
) -> dict[str, str]:
    return {**as_member(jw, email), "Idempotency-Key": key or str(uuid.uuid4())}


async def _queued(jw: JobWorld, slug: str = "acme") -> tuple[uuid.UUID, str]:
    tenant = jw.tenant(slug)
    async with unit_of_work(tenant) as session:
        done = await JobRunRepository(session, tenant).enqueue(
            EnqueueSpec(KIND, "import", uuid.uuid4(), jw.owner(slug))
        )
    assert done.job.queue_job_id is not None
    return done.job.id, done.job.queue_job_id


@pytest.fixture
def hook_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any, str | None]]:
    calls: list[tuple[str, Any, str | None]] = []

    async def hook(session: Any, job: Any, outcome: Any) -> None:
        # read back in the hook's own session: the terminal write is already there
        from sqlalchemy import text

        row = (
            await session.execute(
                text("SELECT status, error_code FROM job_runs WHERE id = :i"), {"i": job.id}
            )
        ).one()
        assert row.status == job.status
        calls.append((job.status, outcome, row.error_code))

    monkeypatch.setitem(job_hooks.TERMINAL_HOOKS, KIND, hook)
    return calls


@pytest.mark.endpoint("jobs_cancel", case="success")
@pytest.mark.parametrize("role", ["owner", "operator"])
async def test_jobs_cancel_success(
    jobs_db: JobWorld, api: httpx.AsyncClient, hook_calls: list[Any], role: str
) -> None:
    job, qid = await _queued(jobs_db)
    response = await api.post(_url(job), headers=_headers(jobs_db, f"{role}@acme.test"))
    assert response.status_code == 200, response.text
    env = JobEnvelope.model_validate(response.json())
    assert (env.job_id, env.status.value) == (job, "cancelled")
    assert env.finished_at is not None
    row = job_row(jobs_db.conn, job)
    assert (row["status"], row["attempt"]) == ("cancelled", 0)
    assert queue_row(jobs_db.conn, qid)["status"] == "cancelled"
    assert hook_calls == [("cancelled", None, None)]
    assert get_queue_app().connector._async_pool is None  # type: ignore[attr-defined]


async def test_jobs_cancel_queued_never_runs(
    jobs_db: JobWorld, api: httpx.AsyncClient, override_body: Any
) -> None:
    runs: list[int] = []

    async def body(ctx: JobContext) -> Outcome:
        runs.append(1)
        return Outcome("succeeded")

    override_body(KIND, body)
    job, qid = await _queued(jobs_db)
    assert (await api.post(_url(job), headers=_headers(jobs_db))).status_code == 200
    # even a stale delivery of the entry is dropped by the claim CAS
    jobs_db.conn.execute("UPDATE procrastinate_jobs SET status = 'todo' WHERE id = %s", [int(qid)])
    async with running_worker():
        await eventually(lambda: queue_row(jobs_db.conn, qid)["status"] == "succeeded")
    assert runs == []
    assert job_row(jobs_db.conn, job)["status"] == "cancelled"


@pytest.mark.endpoint("jobs_cancel", case="conflict")
async def test_jobs_cancel_running_409(jobs_db: JobWorld, api: httpx.AsyncClient) -> None:
    job = insert_job(
        jobs_db.conn,
        jobs_db.tenant("acme"),
        status="running",
        attempt=1,
        started_at=datetime.now(UTC),
        lease_expires_at=datetime(2099, 1, 1, tzinfo=UTC),
    )
    response = await api.post(_url(job), headers=_headers(jobs_db))
    assert response.status_code == 409
    assert response.json()["error"]["type"] == "INVALID_STATUS_TRANSITION"
    assert job_row(jobs_db.conn, job)["status"] == "running"
    assert count(jobs_db.conn, "SELECT count(*) FROM idempotency_keys") == 0


@pytest.mark.endpoint("jobs_cancel", case="conflict")
@pytest.mark.parametrize("status", ["succeeded", "partial", "failed", "cancelled", "dead_letter"])
async def test_jobs_cancel_terminal_409(
    jobs_db: JobWorld, api: httpx.AsyncClient, status: str
) -> None:
    extra: dict[str, Any] = {"status": status, "attempt": 1, "finished_at": datetime.now(UTC)}
    if status in ("failed", "dead_letter"):
        extra["error_report_json"] = {"status": "failed"}
    job = insert_job(jobs_db.conn, jobs_db.tenant("acme"), **extra)
    response = await api.post(_url(job), headers=_headers(jobs_db))
    assert response.status_code == 409
    assert response.json()["error"]["type"] == "INVALID_STATUS_TRANSITION"


@pytest.mark.endpoint("jobs_cancel", case="conflict")
async def test_jobs_cancel_key_reuse_409(jobs_db: JobWorld, api: httpx.AsyncClient) -> None:
    first, _ = await _queued(jobs_db)
    other, _ = await _queued(jobs_db)
    headers = _headers(jobs_db, key="cancel-key-1")
    assert (await api.post(_url(first), headers=headers)).status_code == 200
    response = await api.post(_url(other), headers=headers)
    assert response.status_code == 409
    assert response.json()["error"]["type"] == "IDEMPOTENCY_KEY_REUSED"
    assert job_row(jobs_db.conn, other)["status"] == "queued"
    operator = _headers(jobs_db, "operator@acme.test", key="cancel-key-1")
    assert (await api.post(_url(first), headers=operator)).status_code == 409


async def test_jobs_cancel_replay_returns_first_response(
    jobs_db: JobWorld, api: httpx.AsyncClient, hook_calls: list[Any]
) -> None:
    job, _ = await _queued(jobs_db)
    headers = _headers(jobs_db, key="cancel-replay")
    first = await api.post(_url(job), headers=headers)
    again = await api.post(_url(job), headers=headers)
    assert (first.status_code, again.status_code) == (200, 200)
    assert first.json() == again.json()
    assert len(hook_calls) == 1


async def test_jobs_cancel_race_with_claim_one_wins(
    jobs_db: JobWorld, api: httpx.AsyncClient, override_body: Any
) -> None:
    runs: list[int] = []

    async def body(ctx: JobContext) -> Outcome:
        runs.append(1)
        return Outcome("succeeded")

    override_body(KIND, body)
    for _ in range(5):
        job, _qid = await _queued(jobs_db)
        response, _ = await asyncio.gather(
            api.post(_url(job), headers=_headers(jobs_db)), wrapper.run_job(str(job))
        )
        status = job_row(jobs_db.conn, job)["status"]
        if response.status_code == 200:
            assert status == "cancelled"
        else:
            assert response.status_code == 409 and status == "succeeded"
    assert len(runs) == count(
        jobs_db.conn, "SELECT count(*) FROM job_runs WHERE status = 'succeeded'"
    )


@pytest.mark.endpoint("jobs_cancel", case="wrong_role")
@pytest.mark.parametrize("role", ["reviewer", "admin"])
async def test_jobs_cancel_wrong_role_403(
    jobs_db: JobWorld, api: httpx.AsyncClient, role: str
) -> None:
    job, _ = await _queued(jobs_db)
    response = await api.post(_url(job), headers=_headers(jobs_db, f"{role}@acme.test"))
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "PERMISSION_DENIED"
    assert job_row(jobs_db.conn, job)["status"] == "queued"


@pytest.mark.endpoint("jobs_cancel", case="wrong_tenant")
async def test_jobs_cancel_other_tenant_404(jobs_db: JobWorld, api: httpx.AsyncClient) -> None:
    theirs, qid = await _queued(jobs_db, "bharat")
    response = await api.post(_url(theirs), headers=_headers(jobs_db))
    assert response.status_code == 404
    assert job_row(jobs_db.conn, theirs)["status"] == "queued"
    assert queue_row(jobs_db.conn, qid)["status"] == "todo"


@pytest.mark.endpoint("jobs_cancel", case="not_found")
async def test_jobs_cancel_unknown_404(jobs_db: JobWorld, api: httpx.AsyncClient) -> None:
    response = await api.post(_url(uuid.uuid4()), headers=_headers(jobs_db))
    assert response.status_code == 404
    assert count(jobs_db.conn, "SELECT count(*) FROM idempotency_keys") == 0


@pytest.mark.endpoint("jobs_cancel", case="validation")
async def test_jobs_cancel_validation_422(jobs_db: JobWorld, api: httpx.AsyncClient) -> None:
    job, _ = await _queued(jobs_db)
    bad_id = await api.post(_url("nope"), headers=_headers(jobs_db))
    no_key = await api.post(_url(job), headers=as_member(jobs_db, "owner@acme.test"))
    bad_key = await api.post(_url(job), headers=_headers(jobs_db, key="has space"))
    assert [r.status_code for r in (bad_id, no_key, bad_key)] == [422, 422, 422]
    assert job_row(jobs_db.conn, job)["status"] == "queued"


@pytest.mark.endpoint("jobs_cancel", case="missing_auth")
async def test_jobs_cancel_missing_auth_401(jobs_db: JobWorld, api: httpx.AsyncClient) -> None:
    job, _ = await _queued(jobs_db)
    response = await api.post(_url(job), headers={"Idempotency-Key": "k"})
    assert response.status_code == 401


@pytest.mark.endpoint("jobs_cancel", case="invalid_token", variant="expired")
@pytest.mark.endpoint("jobs_cancel", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("jobs_cancel", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_jobs_cancel_invalid_token_401(
    jobs_db: JobWorld, api: httpx.AsyncClient, variant: str
) -> None:
    job, _ = await _queued(jobs_db)
    headers = {**bad_token(jobs_db, variant), "Idempotency-Key": "k"}
    assert (await api.post(_url(job), headers=headers)).status_code == 401


async def test_hook_receives_none_outcome_on_cancel_and_reap(
    jobs_db: JobWorld,
    api: httpx.AsyncClient,
    override_body: Any,
    hook_calls: list[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.workers.policy import POLICIES, JobPolicy

    # cancel
    job, _ = await _queued(jobs_db)
    assert (await api.post(_url(job), headers=_headers(jobs_db))).status_code == 200
    # a non-running tenant's queued job
    stopped = add_tenant(jobs_db.conn, "stopped-co", status="suspended")
    await wrapper.run_job(str(insert_job(jobs_db.conn, stopped)))
    # the reaper's dead-letter
    dead = insert_job(
        jobs_db.conn,
        jobs_db.tenant("acme"),
        status="running",
        attempt=3,
        started_at=datetime.now(UTC),
        lease_expires_at=datetime(2000, 1, 1, tzinfo=UTC),
    )
    await reap_once()
    assert job_row(jobs_db.conn, dead)["status"] == "dead_letter"
    # timeout, retry exhaustion and an exception
    policies = dict(POLICIES)
    p = policies[KIND]
    policies[KIND] = JobPolicy(p.kind, p.queue, 1, p.max_attempts, p.retry_transport)
    monkeypatch.setattr(wrapper, "POLICIES", policies)

    async def slow(ctx: JobContext) -> Outcome:
        await asyncio.sleep(10)
        return Outcome("succeeded")

    override_body(KIND, slow)
    await wrapper.run_job(str(insert_job(jobs_db.conn, jobs_db.tenant("acme"))))

    async def transport(ctx: JobContext) -> Outcome:
        raise RetryableJobError(ErrorCode.MARKETPLACE_UNAVAILABLE)

    override_body(KIND, transport)
    await wrapper.run_job(str(insert_job(jobs_db.conn, jobs_db.tenant("acme"), max_attempts=1)))

    async def broken(ctx: JobContext) -> Outcome:
        raise RuntimeError("x")

    override_body(KIND, broken)
    await wrapper.run_job(str(insert_job(jobs_db.conn, jobs_db.tenant("acme"))))

    async def fine(ctx: JobContext) -> Outcome:
        return Outcome("partial", {"n": 1})

    override_body(KIND, fine)
    await wrapper.run_job(str(insert_job(jobs_db.conn, jobs_db.tenant("acme"))))
    assert hook_calls == [
        ("cancelled", None, None),
        ("cancelled", None, None),
        ("dead_letter", None, "JOB_TIMEOUT"),
        ("failed", None, "JOB_TIMEOUT"),
        ("dead_letter", None, "RETRY_EXHAUSTED"),
        ("failed", None, "INTERNAL_ERROR"),
        ("partial", Outcome("partial", {"n": 1}), None),
    ]


async def test_hook_sees_final_status(
    jobs_db: JobWorld, override_body: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import text

    seen: list[tuple[Any, ...]] = []

    async def hook(session: Any, job: Any, outcome: Any) -> None:
        row = (
            await session.execute(
                text(
                    "SELECT status, result_json, error_report_json IS NULL, finished_at IS NOT NULL "
                    "FROM job_runs WHERE id = :i"
                ),
                {"i": job.id},
            )
        ).one()
        seen.append((tuple(row), job.status, job.result_json))

    monkeypatch.setitem(job_hooks.TERMINAL_HOOKS, KIND, hook)

    async def body(ctx: JobContext) -> Outcome:
        return Outcome("succeeded", {"done": True})

    override_body(KIND, body)
    await wrapper.run_job(str(insert_job(jobs_db.conn, jobs_db.tenant("acme"))))
    assert seen == [(("succeeded", {"done": True}, True, True), "succeeded", {"done": True})]
