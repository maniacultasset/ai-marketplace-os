"""S13 A5 (DW7, AC3-4; DATABASE.md:1027, :1487): the wrapper's claim, the body under its timeout
with no transaction open, the fenced terminal write and the kind's terminal hook, as app_rw."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from app.db.session import dispose_engine, get_engine, unit_of_work
from app.repositories.jobs import JobRunRepository
from app.services import job_hooks
from app.workers import wrapper
from app.workers.errors import JobFailed
from app.workers.policy import POLICIES, JobPolicy
from app.workers.registry import JobContext, Outcome
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource, JobKind
from mkos_schemas.errors import ErrorItem, ErrorReport
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from .auth_support import add_tenant
from .conftest import LOCAL_HOSTS
from .jobs_support import JobWorld, insert_job, job_row, jobs_db, override_body

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["jobs_db", "override_body"]
KIND = JobKind.BULK_IMPORT


@pytest.fixture
def hook_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Outcome | None]]:
    calls: list[tuple[str, Outcome | None]] = []

    async def hook(session: Any, job: Any, outcome: Outcome | None) -> None:
        calls.append((job.status, outcome))

    monkeypatch.setitem(job_hooks.TERMINAL_HOOKS, KIND, hook)
    return calls


def _short_timeout(monkeypatch: pytest.MonkeyPatch, seconds: int) -> None:
    policies = dict(POLICIES)
    p = policies[KIND]
    policies[KIND] = JobPolicy(p.kind, p.queue, seconds, p.max_attempts, p.retry_transport)
    monkeypatch.setattr(wrapper, "POLICIES", policies)


async def test_wrapper_success(
    jobs_db: JobWorld, override_body: Any, hook_calls: list[Any]
) -> None:
    seen: list[JobContext] = []

    async def body(ctx: JobContext) -> Outcome:
        seen.append(ctx)
        return Outcome("succeeded", {"rows": 3})

    override_body(KIND, body)
    job = insert_job(jobs_db.conn, jobs_db.tenant("acme"), payload_json={"import_id": "x"})
    await wrapper.run_job(str(job))
    row = job_row(jobs_db.conn, job)
    assert (row["status"], row["attempt"], row["result_json"]) == ("succeeded", 1, {"rows": 3})
    assert row["finished_at"] is not None and row["lease_expires_at"] is None
    assert row["error_report_json"] is None
    assert [(c.job_run_id, c.tenant_id, c.payload, c.attempt) for c in seen] == [
        (job, jobs_db.tenant("acme"), {"import_id": "x"}, 1)
    ]
    assert hook_calls == [("succeeded", Outcome("succeeded", {"rows": 3}))]


async def test_wrapper_partial(jobs_db: JobWorld, override_body: Any) -> None:
    async def body(ctx: JobContext) -> Outcome:
        return Outcome("partial", {"failed": 1})

    override_body(KIND, body)
    job = insert_job(jobs_db.conn, jobs_db.tenant("acme"))
    await wrapper.run_job(str(job))
    row = job_row(jobs_db.conn, job)
    assert (row["status"], row["result_json"]) == ("partial", {"failed": 1})


async def test_job_timeout(
    jobs_db: JobWorld,
    override_body: Any,
    hook_calls: list[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _short_timeout(monkeypatch, 2)

    async def body(ctx: JobContext) -> Outcome:
        await asyncio.sleep(30)
        return Outcome("succeeded")

    override_body(KIND, body)
    job = insert_job(jobs_db.conn, jobs_db.tenant("acme"))
    started = asyncio.get_running_loop().time()
    await wrapper.run_job(str(job))
    assert asyncio.get_running_loop().time() - started < 10
    row = job_row(jobs_db.conn, job)
    assert (row["status"], row["attempt"], row["error_code"]) == ("failed", 1, "JOB_TIMEOUT")
    report = ErrorReport.model_validate(row["error_report_json"])
    assert report.error_code is ErrorCode.JOB_TIMEOUT
    assert report.errors[0].source is ErrorSource.SYSTEM
    assert report.retryable is False
    assert hook_calls == [("failed", None)]


async def test_wrapper_body_raising_timeout_error_is_internal(
    jobs_db: JobWorld, override_body: Any
) -> None:
    async def body(ctx: JobContext) -> Outcome:
        raise TimeoutError("socket read")

    override_body(KIND, body)
    job = insert_job(jobs_db.conn, jobs_db.tenant("acme"))
    await wrapper.run_job(str(job))
    row = job_row(jobs_db.conn, job)
    assert (row["status"], row["error_code"]) == ("failed", "INTERNAL_ERROR")


async def test_wrapper_job_failed_not_retried(
    jobs_db: JobWorld, override_body: Any, hook_calls: list[Any]
) -> None:
    report = ErrorReport(
        status="failed",
        error_code=ErrorCode.IMPORT_FAILED,
        errors=[
            ErrorItem(
                field="file.header",
                error_code=ErrorCode.IMPORT_FAILED,
                message="bad header",
                source=ErrorSource.SCHEMA,
                retryable=False,
            )
        ],
    )

    async def body(ctx: JobContext) -> Outcome:
        raise JobFailed(report)

    override_body(KIND, body)
    job = insert_job(jobs_db.conn, jobs_db.tenant("acme"))
    await wrapper.run_job(str(job))  # returns: nothing for the queue to retry
    row = job_row(jobs_db.conn, job)
    assert (row["status"], row["attempt"], row["error_code"]) == ("failed", 1, "IMPORT_FAILED")
    assert ErrorReport.model_validate(row["error_report_json"]) == report
    assert hook_calls == [("failed", None)]


async def test_wrapper_unknown_exception_no_traceback_text(
    jobs_db: JobWorld, override_body: Any, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "cell-value-canary-4242"

    async def body(ctx: JobContext) -> Outcome:
        raise KeyError(secret)

    override_body(KIND, body)
    job = insert_job(jobs_db.conn, jobs_db.tenant("acme"))
    await wrapper.run_job(str(job))
    row = job_row(jobs_db.conn, job)
    assert (row["status"], row["error_code"]) == ("failed", "INTERNAL_ERROR")
    report = ErrorReport.model_validate(row["error_report_json"])
    assert report.errors[0].message == "the job failed (KeyError)"
    assert secret not in str(row["error_report_json"])
    assert "Traceback" not in str(row["error_report_json"])
    assert secret not in caplog.text


async def test_wrapper_no_body_failed(jobs_db: JobWorld, override_body: Any) -> None:
    from app.workers import registry

    registry.REGISTRY.pop(KIND, None)
    job = insert_job(jobs_db.conn, jobs_db.tenant("acme"))
    await wrapper.run_job(str(job))
    row = job_row(jobs_db.conn, job)
    assert (row["status"], row["error_code"], row["attempt"]) == ("failed", "INTERNAL_ERROR", 1)


async def test_wrapper_bad_or_unknown_id_dropped(jobs_db: JobWorld) -> None:
    await wrapper.run_job("not-a-uuid")
    await wrapper.run_job(str(uuid.uuid4()))


async def test_claim_cas_drops_duplicate(jobs_db: JobWorld, override_body: Any) -> None:
    runs: list[int] = []

    async def body(ctx: JobContext) -> Outcome:
        runs.append(ctx.attempt)
        await asyncio.sleep(0.3)
        return Outcome("succeeded")

    override_body(KIND, body)
    job = insert_job(jobs_db.conn, jobs_db.tenant("acme"))
    await asyncio.gather(wrapper.run_job(str(job)), wrapper.run_job(str(job)))
    assert runs == [1]
    assert job_row(jobs_db.conn, job)["status"] == "succeeded"


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled", "dead_letter"])
async def test_wrapper_claim_skips_non_queued(
    jobs_db: JobWorld, override_body: Any, status: str
) -> None:
    runs: list[int] = []

    async def body(ctx: JobContext) -> Outcome:
        runs.append(1)
        return Outcome("succeeded")

    override_body(KIND, body)
    extra: dict[str, Any] = {"status": status, "finished_at": datetime.now(UTC)}
    if status in ("failed", "dead_letter"):
        extra["error_report_json"] = {"status": "failed"}
    job = insert_job(jobs_db.conn, jobs_db.tenant("acme"), **extra)
    await wrapper.run_job(str(job))
    assert runs == []
    assert job_row(jobs_db.conn, job)["status"] == status


async def test_worker_tenant_context(jobs_db: JobWorld, override_body: Any) -> None:
    acme, bharat = jobs_db.tenant("acme"), jobs_db.tenant("bharat")
    other = insert_job(jobs_db.conn, bharat)
    seen: dict[str, Any] = {}

    async def body(ctx: JobContext) -> Outcome:
        async with unit_of_work(ctx.tenant_id) as session:
            ids = (await session.execute(text("SELECT tenant_id FROM job_runs"))).scalars()
            seen["tenants"] = set(ids)
            seen["other"] = (
                await session.execute(text("SELECT id FROM job_runs WHERE id = :i"), {"i": other})
            ).all()
        try:
            async with unit_of_work(ctx.tenant_id) as session:
                await session.execute(
                    text(
                        "INSERT INTO job_runs (tenant_id, kind, entity_type, entity_id, "
                        "payload_json, correlation_id) VALUES (:t, 'bulk_import', 'import', "
                        "gen_random_uuid(), '{}', gen_random_uuid())"
                    ),
                    {"t": bharat},
                )
        except DBAPIError as exc:
            seen["write_b"] = getattr(exc.orig, "sqlstate", None)
        return Outcome("succeeded")

    override_body(KIND, body)
    job = insert_job(jobs_db.conn, acme)
    await dispose_engine()
    await wrapper.run_job(str(job))
    assert seen == {"tenants": {acme}, "other": [], "write_b": "42501"}
    async with get_engine().connect() as conn:
        guc = (await conn.execute(text("SELECT current_setting('app.tenant_id', true)"))).scalar()
    assert guc in (None, "")
    assert job_row(jobs_db.conn, job)["status"] == "succeeded"


async def test_no_transaction_open_during_body(jobs_db: JobWorld, override_body: Any) -> None:
    states: list[int] = []

    async def body(ctx: JobContext) -> Outcome:
        row = jobs_db.conn.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE usename = 'app_rw' "
            "AND datname = current_database() AND state LIKE 'idle in transaction%%'"
        ).fetchone()
        assert row is not None
        states.append(int(row[0]))
        return Outcome("succeeded")

    override_body(KIND, body)
    job = insert_job(jobs_db.conn, jobs_db.tenant("acme"))
    await wrapper.run_job(str(job))
    assert states == [0]


async def test_terminal_row_unchanged_by_second_finish(
    jobs_db: JobWorld, override_body: Any
) -> None:
    async def body(ctx: JobContext) -> Outcome:
        return Outcome("succeeded", {"n": 1})

    override_body(KIND, body)
    tenant = jobs_db.tenant("acme")
    job = insert_job(jobs_db.conn, tenant)
    await wrapper.run_job(str(job))
    before = job_row(jobs_db.conn, job)
    async with unit_of_work(tenant) as session:
        again = await JobRunRepository(session, tenant).finish(job, 1, status="failed")
    assert again is None
    assert job_row(jobs_db.conn, job) == before
    # the S10 guard refuses any write to a terminal row, even past the fence
    with pytest.raises(psycopg.Error) as caught:
        jobs_db.conn.execute("UPDATE job_runs SET result_json = '{}' WHERE id = %s", [job])
    assert caught.value.sqlstate == "GRD01"


async def test_stale_worker_finish_after_reap_is_dropped(
    jobs_db: JobWorld, override_body: Any, hook_calls: list[Any]
) -> None:
    gates = {1: asyncio.Event(), 2: asyncio.Event()}
    started = {1: asyncio.Event(), 2: asyncio.Event()}

    async def body(ctx: JobContext) -> Outcome:
        started[ctx.attempt].set()
        await gates[ctx.attempt].wait()
        return Outcome("succeeded", {"attempt": ctx.attempt})

    override_body(KIND, body)
    job = insert_job(jobs_db.conn, jobs_db.tenant("acme"))
    first = asyncio.create_task(wrapper.run_job(str(job)))
    await asyncio.wait_for(started[1].wait(), 10)
    # the reaper's move: the lease expired, back to queued with the attempt kept
    jobs_db.conn.execute(
        "UPDATE job_runs SET status = 'queued', lease_expires_at = NULL WHERE id = %s", [job]
    )
    second = asyncio.create_task(wrapper.run_job(str(job)))
    await asyncio.wait_for(started[2].wait(), 10)
    gates[1].set()
    await asyncio.wait_for(first, 10)
    row = job_row(jobs_db.conn, job)
    assert (row["status"], row["attempt"]) == ("running", 2)
    assert hook_calls == []
    gates[2].set()
    await asyncio.wait_for(second, 10)
    row = job_row(jobs_db.conn, job)
    assert (row["status"], row["attempt"], row["result_json"]) == (
        "succeeded",
        2,
        {"attempt": 2},
    )
    assert hook_calls == [("succeeded", Outcome("succeeded", {"attempt": 2}))]


@pytest.mark.parametrize("status", ["suspended", "offboarded"])
async def test_claim_skips_suspended_tenant(
    jobs_db: JobWorld, override_body: Any, hook_calls: list[Any], status: str
) -> None:
    runs: list[int] = []

    async def body(ctx: JobContext) -> Outcome:
        runs.append(1)
        return Outcome("succeeded")

    override_body(KIND, body)
    tenant = add_tenant(jobs_db.conn, f"stopped-{status}", status=status)
    job = insert_job(jobs_db.conn, tenant)
    await wrapper.run_job(str(job))
    await wrapper.run_job(str(job))  # a redelivery changes nothing
    assert runs == []
    row = job_row(jobs_db.conn, job)
    assert (row["status"], row["attempt"]) == ("cancelled", 0)
    assert row["finished_at"] is not None
    assert hook_calls == [("cancelled", None)]


async def test_claim_trial_tenant_runs(jobs_db: JobWorld, override_body: Any) -> None:
    async def body(ctx: JobContext) -> Outcome:
        return Outcome("succeeded")

    override_body(KIND, body)
    tenant = add_tenant(jobs_db.conn, "trialco", status="trial")
    job = insert_job(jobs_db.conn, tenant)
    await wrapper.run_job(str(job))
    assert job_row(jobs_db.conn, job)["status"] == "succeeded"
