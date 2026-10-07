"""S13 A6 (DW7, AC4, Q1): transport failures back off and stop at `max_attempts` with
`dead_letter`; generation is never retried for a domain failure; a shutdown abort goes back to
`queued` uncounted however often it happens. A real procrastinate worker, as app_rw."""

from __future__ import annotations

import asyncio
import itertools
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from app.workers import wrapper
from app.workers.errors import JobFailed, RetryableJobError, RetryLater, system_report
from app.workers.registry import JobContext, Outcome
from app.workers.retry import INFRA_RETRY_CAP, JobRetryStrategy
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import JobKind
from mkos_schemas.errors import ErrorReport
from procrastinate.exceptions import JobAborted
from sqlalchemy.exc import OperationalError

from . import factories as f
from .conftest import LOCAL_HOSTS
from .jobs_support import (
    JobWorld,
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


def test_retry_strategy_decisions() -> None:
    strategy = JobRetryStrategy()
    job: Any = object()
    later = strategy.get_retry_decision(exception=RetryLater(5.0), job=job)
    assert later is not None and later.retry_at is not None
    now = strategy.get_retry_decision(exception=asyncio.CancelledError(), job=job)
    assert now is not None
    assert later.retry_at > now.retry_at  # type: ignore[operator]
    assert strategy.get_retry_decision(exception=JobAborted(), job=job) is not None


def _queue_job(attempts: int) -> Any:
    """A stand-in for procrastinate's `Job`: the strategy reads only its `attempts`."""
    return SimpleNamespace(attempts=attempts)


def test_retry_strategy_infrastructure_error_backs_off_up_to_the_cap() -> None:
    """S13 fix F2: an exception that escapes the wrapper (a DB error while naming or claiming
    the job) is retried with backoff, so the `queued` row is never left with a dead entry."""
    strategy = JobRetryStrategy()
    boom = OperationalError("SELECT 1", {}, Exception("connection reset"))
    for other in (boom, RuntimeError("x"), KeyError()):
        before = datetime.now(UTC)
        first = strategy.get_retry_decision(exception=other, job=_queue_job(0))
        assert first is not None and first.retry_at is not None
        assert before + timedelta(seconds=0.5) <= first.retry_at
        assert first.retry_at <= datetime.now(UTC) + timedelta(seconds=1)
    late = strategy.get_retry_decision(exception=boom, job=_queue_job(INFRA_RETRY_CAP - 1))
    assert late is not None and late.retry_at is not None
    assert late.retry_at <= datetime.now(UTC) + timedelta(seconds=60)
    assert strategy.get_retry_decision(exception=boom, job=_queue_job(INFRA_RETRY_CAP)) is None
    # a non-Exception BaseException (interpreter exit) is never retried
    assert strategy.get_retry_decision(exception=KeyboardInterrupt(), job=_queue_job(0)) is None


@pytest.mark.parametrize("where", ["resolve_job", "claim"])
async def test_infrastructure_error_before_the_body_is_retried(
    jobs_db: JobWorld, override_body: Any, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    """A DB blip in `resolve_job` or the claim once: the queue retries the entry, the row is
    claimed on the redelivery and the job succeeds (it is never left `queued` with a dead entry)."""
    from app.db.job_context import resolve_job
    from app.repositories.jobs import JobRunRepository

    runs: list[int] = []

    async def body(ctx: JobContext) -> Outcome:
        runs.append(ctx.attempt)
        return Outcome("succeeded")

    override_body(JobKind.BULK_IMPORT, body)
    failed: list[str] = []

    def once(real: Any) -> Any:
        async def flaky(*args: Any, **kwargs: Any) -> Any:
            if not failed:
                failed.append(where)
                raise OperationalError("SELECT 1", {}, Exception("connection reset"))
            return await real(*args, **kwargs)

        return flaky

    if where == "resolve_job":
        monkeypatch.setattr("app.workers.wrapper.resolve_job", once(resolve_job))
    else:
        monkeypatch.setattr(JobRunRepository, "claim", once(JobRunRepository.claim))
    job, qid = await _enqueue(jobs_db, jobs_db.tenant("acme"))
    async with running_worker(poll_s=0.2):
        await eventually(lambda: job_row(jobs_db.conn, job)["status"] == "succeeded", 15)
    assert failed == [where]
    assert runs == [1]
    row = job_row(jobs_db.conn, job)
    assert row["attempt"] == 1
    entry = queue_row(jobs_db.conn, qid)
    assert (entry["status"], entry["attempts"]) == ("succeeded", 2)


async def _enqueue(jw: JobWorld, tenant: uuid.UUID, **columns: Any) -> tuple[uuid.UUID, str]:
    """A job row plus its queue entry, through the real enqueue helper."""
    from app.db.session import unit_of_work
    from app.repositories.jobs import EnqueueSpec, JobRunRepository

    kind = JobKind(columns.pop("kind", "bulk_import"))
    async with unit_of_work(tenant) as session:
        done = await JobRunRepository(session, tenant).enqueue(
            EnqueueSpec(
                kind=kind,
                entity_type=columns.pop("entity_type", "import"),
                entity_id=columns.pop("entity_id", uuid.uuid4()),
                created_by=columns.pop("created_by", None),
                listing_id=columns.pop("listing_id", None),
            )
        )
    assert done.job.queue_job_id is not None
    return done.job.id, done.job.queue_job_id


def _generation_job(jw: JobWorld) -> tuple[uuid.UUID, dict[str, Any]]:
    tenant = jw.tenant("acme")
    listing = f.listing(jw.conn, tenant)
    return tenant, {
        "kind": "listing_generate",
        "entity_type": "listing",
        "entity_id": listing,
        "listing_id": listing,
    }


async def test_job_retry_policy_transport_backs_off_then_dead_letter(
    jobs_db: JobWorld, override_body: Any
) -> None:
    runs: list[float] = []

    async def body(ctx: JobContext) -> Outcome:
        runs.append(asyncio.get_running_loop().time())
        raise RetryableJobError(ErrorCode.MARKETPLACE_UNAVAILABLE)

    override_body(JobKind.BULK_IMPORT, body)
    job, qid = await _enqueue(jobs_db, jobs_db.tenant("acme"))
    async with running_worker():
        await eventually(lambda: job_row(jobs_db.conn, job)["status"] == "dead_letter", 20)
        await asyncio.sleep(1.5)  # nothing reschedules after the cap
    row = job_row(jobs_db.conn, job)
    assert (row["attempt"], row["error_code"]) == (3, "RETRY_EXHAUSTED")
    assert ErrorReport.model_validate(row["error_report_json"]).attempts_used == 3
    assert len(runs) == 3
    assert queue_row(jobs_db.conn, qid)["status"] == "succeeded"


async def test_job_retry_policy_backoff_gaps(jobs_db: JobWorld, override_body: Any) -> None:
    runs: list[float] = []

    async def body(ctx: JobContext) -> Outcome:
        runs.append(asyncio.get_running_loop().time())
        if ctx.attempt < 3:
            raise RetryableJobError(ErrorCode.MARKETPLACE_UNAVAILABLE)
        return Outcome("succeeded")

    override_body(JobKind.BULK_IMPORT, body)
    job, _ = await _enqueue(jobs_db, jobs_db.tenant("acme"))
    async with running_worker(poll_s=0.2):
        await eventually(lambda: job_row(jobs_db.conn, job)["status"] == "succeeded", 20)
    gaps = [b - a for a, b in itertools.pairwise(runs)]
    assert len(gaps) == 2
    # backoff_s: attempt 1 waits 0.5-1 s, attempt 2 waits 1-2 s (jitter), plus one poll
    assert 0.5 <= gaps[0] <= 1.0 + 1.0
    assert 1.0 <= gaps[1] <= 2.0 + 1.0
    assert job_row(jobs_db.conn, job)["attempt"] == 3


async def test_job_retry_policy_generation_domain_failure_not_retried(
    jobs_db: JobWorld, override_body: Any
) -> None:
    runs: list[int] = []

    async def body(ctx: JobContext) -> Outcome:
        runs.append(ctx.attempt)
        raise JobFailed(
            system_report(
                ErrorCode.GENERATION_FAILED, "no", attempt=1, max_attempts=1, request_id="r"
            )
        )

    override_body(JobKind.LISTING_GENERATE, body)
    tenant, columns = _generation_job(jobs_db)
    job, qid = await _enqueue(jobs_db, tenant, **columns)
    async with running_worker():
        await eventually(lambda: job_row(jobs_db.conn, job)["status"] == "failed")
        await asyncio.sleep(0.5)
    row = job_row(jobs_db.conn, job)
    assert (row["attempt"], row["max_attempts"], row["error_code"]) == (1, 1, "GENERATION_FAILED")
    assert runs == [1]
    assert queue_row(jobs_db.conn, qid)["status"] == "succeeded"


async def test_job_retry_policy_generation_transport_dead_letters_at_1(
    jobs_db: JobWorld, override_body: Any
) -> None:
    runs: list[int] = []

    async def body(ctx: JobContext) -> Outcome:
        runs.append(ctx.attempt)
        raise RetryableJobError(ErrorCode.MARKETPLACE_UNAVAILABLE)

    override_body(JobKind.LISTING_GENERATE, body)
    tenant, columns = _generation_job(jobs_db)
    job, _ = await _enqueue(jobs_db, tenant, **columns)
    async with running_worker():
        await eventually(lambda: job_row(jobs_db.conn, job)["status"] == "dead_letter")
        await asyncio.sleep(0.5)
    row = job_row(jobs_db.conn, job)
    assert (row["attempt"], row["error_code"]) == (1, "RETRY_EXHAUSTED")
    assert runs == [1]


async def _blocking_body(started: asyncio.Event, release: asyncio.Event | None = None) -> Any:
    async def body(ctx: JobContext) -> Outcome:
        started.set()
        if release is None:
            await asyncio.sleep(3600)
        else:
            await release.wait()
        return Outcome("succeeded")

    return body


async def test_shutdown_abort_requeues_uncounted(jobs_db: JobWorld, override_body: Any) -> None:
    started = asyncio.Event()
    override_body(JobKind.BULK_IMPORT, await _blocking_body(started))
    job, qid = await _enqueue(jobs_db, jobs_db.tenant("acme"))
    async with running_worker(graceful_s=0.3):
        await asyncio.wait_for(started.wait(), 10)
        assert job_row(jobs_db.conn, job)["status"] == "running"
    row = job_row(jobs_db.conn, job)
    assert (row["status"], row["attempt"], row["lease_expires_at"]) == ("queued", 0, None)
    assert queue_row(jobs_db.conn, qid)["status"] == "todo"


async def test_three_shutdowns_still_run(jobs_db: JobWorld, override_body: Any) -> None:
    # generation: max_attempts = 1, so a counted abort would exhaust it at the first deploy
    tenant, columns = _generation_job(jobs_db)
    job, qid = await _enqueue(jobs_db, tenant, **columns)
    for _ in range(3):
        started = asyncio.Event()
        override_body(JobKind.LISTING_GENERATE, await _blocking_body(started))
        async with running_worker(graceful_s=0.3):
            await asyncio.wait_for(started.wait(), 10)
        assert job_row(jobs_db.conn, job)["status"] == "queued"
    attempts: list[int] = []

    async def finish(ctx: JobContext) -> Outcome:
        attempts.append(ctx.attempt)
        return Outcome("succeeded")

    override_body(JobKind.LISTING_GENERATE, finish)
    async with running_worker():
        await eventually(lambda: job_row(jobs_db.conn, job)["status"] == "succeeded")
    assert attempts == [1]
    assert job_row(jobs_db.conn, job)["attempt"] == 1
    assert queue_row(jobs_db.conn, qid)["attempts"] == 4


async def test_stale_shutdown_release_after_reap_is_dropped(
    jobs_db: JobWorld, override_body: Any
) -> None:
    started = {1: asyncio.Event(), 2: asyncio.Event()}
    release = asyncio.Event()

    async def body(ctx: JobContext) -> Outcome:
        started[ctx.attempt].set()
        if ctx.attempt == 1:
            await asyncio.sleep(3600)
        await release.wait()
        return Outcome("succeeded")

    override_body(JobKind.BULK_IMPORT, body)
    job = insert_job(jobs_db.conn, jobs_db.tenant("acme"))
    first = asyncio.create_task(wrapper.run_job(str(job)))
    await asyncio.wait_for(started[1].wait(), 10)
    jobs_db.conn.execute(
        "UPDATE job_runs SET status = 'queued', lease_expires_at = NULL WHERE id = %s", [job]
    )
    second = asyncio.create_task(wrapper.run_job(str(job)))
    await asyncio.wait_for(started[2].wait(), 10)
    first.cancel()  # the old worker's shutdown abort
    with pytest.raises(asyncio.CancelledError):
        await first
    row = job_row(jobs_db.conn, job)
    assert (row["status"], row["attempt"]) == ("running", 2)
    release.set()
    await asyncio.wait_for(second, 10)
    assert (job_row(jobs_db.conn, job)["status"], job_row(jobs_db.conn, job)["attempt"]) == (
        "succeeded",
        2,
    )
