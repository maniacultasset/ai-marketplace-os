"""S18 T8 (AC 2, 14; D12; K2, CP11): the spine extension. A body's `persist` runs in the one fenced
terminal transaction, the outcome may zero the budget reservation, and a persist that raises rolls
the whole transaction back and falls back to `failed` + `INTERNAL_ERROR`, as app_rw."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from app.workers import wrapper
from app.workers.errors import JobFailed
from app.workers.registry import JobContext, Outcome
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource, JobKind
from mkos_schemas.errors import ErrorItem, ErrorReport

from . import factories as f
from .conftest import LOCAL_HOSTS
from .jobs_support import JobWorld, insert_job, job_row, jobs_db, override_body

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["jobs_db", "override_body"]
KIND = JobKind.LISTING_GENERATE


def _job(jw: JobWorld, **columns: Any) -> uuid.UUID:
    acme = jw.tenant("acme")
    listing = f.listing(jw.conn, acme)
    return insert_job(
        jw.conn,
        acme,
        kind=KIND.value,
        entity_type="listing",
        entity_id=listing,
        listing_id=listing,
        max_attempts=1,
        cost_reserved_usd=Decimal("0.10"),
        **columns,
    )


def _record(tenant: uuid.UUID, job: uuid.UUID) -> Any:
    from mkos_schemas.agent import AgentRunRecord

    now = datetime.now(UTC)
    return AgentRunRecord(
        id=uuid.uuid4(),
        tenant_id=tenant,
        job_id=job,
        request_id="req-t8",
        agent_name="content",
        trigger="generate",
        model_used="stub",
        status="completed",
        started_at=now,
        created_at=now,
    )


def _agent_rows(jw: JobWorld, job: uuid.UUID) -> int:
    row = jw.conn.execute("SELECT count(*) FROM agent_runs WHERE job_id = %s", [job]).fetchone()
    assert row is not None
    return int(row[0])


def _failed_report() -> ErrorReport:
    item = ErrorItem(
        field="listing",
        error_code=ErrorCode.GENERATION_FAILED,
        message="nothing came back",
        source=ErrorSource.AI_MODEL,
        retryable=False,
    )
    return ErrorReport(status="failed", error_code=ErrorCode.GENERATION_FAILED, errors=[item])


class BoomError(RuntimeError):
    pass


async def test_persist_runs_in_terminal_tx(jobs_db: JobWorld, override_body: Any) -> None:
    acme = jobs_db.tenant("acme")
    job = _job(jobs_db)

    async def persist(uow: Any) -> None:
        await uow.agent_runs.insert(_record(acme, job))
        raise BoomError("secret-ish text")

    async def body(ctx: JobContext) -> Outcome:
        return Outcome("succeeded", {"x": 1}, persist=persist, zero_reservation=True)

    override_body(KIND, body)
    await wrapper.run_job(str(job))
    assert _agent_rows(jobs_db, job) == 0  # the first transaction rolled back whole


async def test_persist_error_fails_job_internal_error(
    jobs_db: JobWorld, override_body: Any
) -> None:
    job = _job(jobs_db)

    async def persist(uow: Any) -> None:
        raise BoomError("do not echo me")

    async def body(ctx: JobContext) -> Outcome:
        return Outcome("succeeded", {"x": 1}, persist=persist, zero_reservation=True)

    override_body(KIND, body)
    await wrapper.run_job(str(job))
    row = job_row(jobs_db.conn, job)
    assert (row["status"], row["error_code"], row["result_json"]) == (
        "failed",
        "INTERNAL_ERROR",
        None,
    )
    assert row["cost_reserved_usd"] == Decimal("0.10")  # the reservation is kept
    report = ErrorReport.model_validate(row["error_report_json"])
    assert report.errors[0].message == "generation failed"
    assert (report.errors[0].field, report.errors[0].source) == ("listing", ErrorSource.SYSTEM)
    text = str(row["error_report_json"])
    assert "BoomError" not in text and "do not echo me" not in text


async def test_persist_error_fallback_raises_leaves_running(
    jobs_db: JobWorld, override_body: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _job(jobs_db)

    async def persist(uow: Any) -> None:
        raise BoomError("first")

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise BoomError("fallback")

    monkeypatch.setattr(wrapper, "fallback_report", broken)

    async def body(ctx: JobContext) -> Outcome:
        return Outcome("succeeded", None, persist=persist)

    override_body(KIND, body)
    await wrapper.run_job(str(job))
    row = job_row(jobs_db.conn, job)
    assert (row["status"], row["lease_expires_at"] is not None) == ("running", True)


async def test_persist_skipped_when_fenced_out(jobs_db: JobWorld, override_body: Any) -> None:
    job = _job(jobs_db)
    called: list[bool] = []

    async def persist(uow: Any) -> None:
        called.append(True)

    async def body(ctx: JobContext) -> Outcome:
        # the reaper took the lease meanwhile: the row is back to queued
        jobs_db.conn.execute(
            "UPDATE job_runs SET status = 'queued', lease_expires_at = NULL, started_at = NULL "
            "WHERE id = %s",
            [job],
        )
        return Outcome("succeeded", None, persist=persist, zero_reservation=True)

    override_body(KIND, body)
    await wrapper.run_job(str(job))
    assert called == []
    row = job_row(jobs_db.conn, job)
    assert (row["status"], row["cost_reserved_usd"]) == ("queued", Decimal("0.10"))


async def test_zero_reservation_on_outcome(jobs_db: JobWorld, override_body: Any) -> None:
    job = _job(jobs_db)

    async def body(ctx: JobContext) -> Outcome:
        return Outcome("succeeded", {"ok": True}, zero_reservation=True)

    override_body(KIND, body)
    await wrapper.run_job(str(job))
    row = job_row(jobs_db.conn, job)
    assert (row["status"], row["cost_reserved_usd"]) == ("succeeded", Decimal("0"))


async def test_no_zero_keeps_reservation(jobs_db: JobWorld, override_body: Any) -> None:
    job = _job(jobs_db)

    async def body(ctx: JobContext) -> Outcome:
        return Outcome("succeeded", {"ok": True})

    override_body(KIND, body)
    await wrapper.run_job(str(job))
    assert job_row(jobs_db.conn, job)["cost_reserved_usd"] == Decimal("0.10")


async def test_timeout_keeps_reservation(
    jobs_db: JobWorld, override_body: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from app.workers.policy import POLICIES, JobPolicy

    policies = dict(POLICIES)
    p = policies[KIND]
    policies[KIND] = JobPolicy(p.kind, p.queue, 1, p.max_attempts, p.retry_transport)
    monkeypatch.setattr(wrapper, "POLICIES", policies)
    job = _job(jobs_db)

    async def body(ctx: JobContext) -> Outcome:
        await asyncio.sleep(30)
        return Outcome("succeeded", zero_reservation=True)

    override_body(KIND, body)
    await wrapper.run_job(str(job))
    row = job_row(jobs_db.conn, job)
    assert (row["status"], row["error_code"]) == ("failed", "JOB_TIMEOUT")
    assert row["cost_reserved_usd"] == Decimal("0.10")


async def test_outcome_error_sets_envelope_error_on_succeeded(
    jobs_db: JobWorld, override_body: Any
) -> None:
    from app.db.models.ops import JobRun
    from app.services.jobs import to_envelope

    job = _job(jobs_db)
    report = _failed_report()

    async def body(ctx: JobContext) -> Outcome:
        return Outcome("succeeded", {"status": "blocked"}, error=report)

    override_body(KIND, body)
    await wrapper.run_job(str(job))
    row = job_row(jobs_db.conn, job)
    assert row["status"] == "succeeded"
    assert ErrorReport.model_validate(row["error_report_json"]).error_code is (
        ErrorCode.GENERATION_FAILED
    )
    envelope = to_envelope(JobRun(**{k: v for k, v in row.items() if hasattr(JobRun, k)}))
    assert envelope.error is not None


async def test_jobfailed_persist_runs(jobs_db: JobWorld, override_body: Any) -> None:
    acme = jobs_db.tenant("acme")
    job = _job(jobs_db)

    async def persist(uow: Any) -> None:
        await uow.agent_runs.insert(_record(acme, job))

    async def body(ctx: JobContext) -> Outcome:
        raise JobFailed(_failed_report(), persist=persist, zero_reservation=True)

    override_body(KIND, body)
    await wrapper.run_job(str(job))
    row = job_row(jobs_db.conn, job)
    assert (row["status"], row["error_code"], row["cost_reserved_usd"]) == (
        "failed",
        "GENERATION_FAILED",
        Decimal("0"),
    )
    assert _agent_rows(jobs_db, job) == 1
