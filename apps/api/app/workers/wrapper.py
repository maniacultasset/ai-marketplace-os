"""The job wrapper (DATABASE.md:1027, :1487; spike REPORT §5 item 3): every queue task runs
`run_job(job_run_id)`.

1. Name the job through `jr_by_id` (`resolve_job`); an unknown id is dropped.
2. Re-bind the origin `request_id` (a fresh one if the row has none) and the tenant.
3. Claim it in one short `unit_of_work`: the compare-and-set `queued → running`, which also needs
   a `trial|active` tenant. 0 rows: a queued job of a stopped tenant is cancelled (and its hook
   runs), anything else is a duplicate or stale delivery and is dropped.
4. Run the body under `asyncio.timeout(policy.timeout_s)` with no transaction open.
5. One short terminal transaction: the outcome, then the kind's terminal hook. Every write is
   fenced on `status = 'running' AND attempt = <claimed>`; 0 rows (the lease was reaped and a
   newer attempt owns the row) logs `job.stale_write_dropped` and writes nothing else.

Outcomes: `Outcome` → `succeeded`/`partial`; timeout → `failed` + `JOB_TIMEOUT`; `JobFailed` →
`failed` with its report; `RetryableJobError` → `queued` and `RetryLater(backoff)` while attempts
are left, else `dead_letter` + `RETRY_EXHAUSTED`; a shutdown abort (`CancelledError`) → back to
`queued` uncounted, then re-raised; anything else → `failed` + `INTERNAL_ERROR` naming only the
exception class. Log lines carry ids and codes only, never payload or cell values.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource, JobKind
from mkos_schemas.errors import ErrorItem, ErrorReport

from app.core.logging import get_logger
from app.core.request_context import bind_tenant, new_request_id, request_context
from app.db.job_context import JobHead, resolve_job
from app.db.session import unit_of_work
from app.repositories.jobs import Claimed, JobRunRepository
from app.services.job_hooks import run_terminal_hook
from app.workers import registry
from app.workers.errors import JobFailed, RetryableJobError, RetryLater
from app.workers.policy import POLICIES, JobPolicy, backoff_s, lease_s
from app.workers.registry import JobContext, Outcome

log = get_logger(__name__)
_rand = secrets.SystemRandom()


def policy_for(kind: JobKind, policies: Mapping[JobKind, JobPolicy] | None = None) -> JobPolicy:
    return (POLICIES if policies is None else policies)[kind]


def _report(
    code: ErrorCode, message: str, *, attempt: int, max_attempts: int, request_id: str
) -> ErrorReport:
    item = ErrorItem(
        field="request",
        error_code=code,
        message=message,
        source=ErrorSource.SYSTEM,
        retryable=False,
    )
    return ErrorReport(
        status="failed",
        error_code=code,
        retryable=False,
        attempts_used=attempt,
        max_attempts=max_attempts,
        errors=[item],
        request_id=request_id,
    )


async def run_job(job_run_id: str) -> None:
    """The body of every queue task. Returns normally unless the body must be retried
    (`RetryLater`) or the worker is shutting down (`CancelledError`)."""
    try:
        job_id = UUID(str(job_run_id))
    except ValueError:
        log.warning("job.bad_id")
        return
    head = await resolve_job(job_id)
    if head is None:
        log.warning("job.unknown", job_run_id=str(job_id))
        return
    with request_context(head.request_id or new_request_id()) as ctx:
        bind_tenant(head.tenant_id)
        kind = JobKind(head.kind)
        policy = policy_for(kind)
        claimed = await _claim(job_id, head, policy)
        if claimed is not None:
            await _execute(job_id, head, kind, policy, claimed, ctx.request_id)


async def _claim(job_id: UUID, head: JobHead, policy: JobPolicy) -> Claimed | None:
    async with unit_of_work(head.tenant_id) as session:
        repo = JobRunRepository(session, head.tenant_id)
        claimed = await repo.claim(job_id, lease_s(policy))
        if claimed is not None:
            log.info("job.claimed", job_run_id=str(job_id), kind=head.kind, attempt=claimed.attempt)
            return claimed
        cancelled = await repo.cancel_non_running(job_id)
        if cancelled is not None:
            await run_terminal_hook(session, cancelled, None)
            log.info("job.cancelled_tenant_not_running", job_run_id=str(job_id))
        else:
            log.info("job.claim_dropped", job_run_id=str(job_id))
    return None


async def _execute(
    job_id: UUID,
    head: JobHead,
    kind: JobKind,
    policy: JobPolicy,
    claimed: Claimed,
    request_id: str,
) -> None:
    attempt = claimed.attempt
    max_attempts = claimed.job.max_attempts
    body = registry.body_for(kind)
    context = JobContext(
        job_run_id=job_id,
        tenant_id=head.tenant_id,
        kind=kind,
        request_id=request_id,
        payload=dict(claimed.job.payload_json or {}),
        attempt=attempt,
    )

    def failure(code: ErrorCode, message: str) -> ErrorReport:
        return _report(
            code, message, attempt=attempt, max_attempts=max_attempts, request_id=request_id
        )

    if body is None:
        await _finish(
            head,
            job_id,
            attempt,
            "failed",
            None,
            failure(ErrorCode.INTERNAL_ERROR, f"no body is registered for {kind.value}"),
        )
        return
    try:
        async with asyncio.timeout(policy.timeout_s) as deadline:
            try:
                outcome = await body(context)
            except TimeoutError:
                if deadline.expired():
                    raise
                raise _BodyTimeoutError from None
    except TimeoutError:
        log.warning("job.timeout", job_run_id=str(job_id), timeout_s=policy.timeout_s)
        await _finish(
            head,
            job_id,
            attempt,
            "failed",
            None,
            failure(ErrorCode.JOB_TIMEOUT, f"the job exceeded its {policy.timeout_s} s timeout"),
        )
        return
    except JobFailed as exc:
        await _finish(head, job_id, attempt, "failed", None, exc.report)
        return
    except RetryableJobError as exc:
        await _retry_or_dead_letter(head, job_id, policy, attempt, max_attempts, exc, failure)
        return
    except asyncio.CancelledError:
        await asyncio.shield(_release_shutdown(head, job_id, attempt))
        raise
    except Exception as exc:  # the body's own failure: recorded, never re-raised to the queue
        name = "TimeoutError" if isinstance(exc, _BodyTimeoutError) else type(exc).__name__
        log.warning("job.body_failed", job_run_id=str(job_id), error_class=name)
        await _finish(
            head,
            job_id,
            attempt,
            "failed",
            None,
            failure(ErrorCode.INTERNAL_ERROR, f"the job failed ({name})"),
        )
        return
    await _finish(head, job_id, attempt, outcome.status, outcome, None)


class _BodyTimeoutError(Exception):
    """A `TimeoutError` the body raised itself (not our deadline): an ordinary failure."""


async def _finish(
    head: JobHead,
    job_id: UUID,
    attempt: int,
    status: str,
    outcome: Outcome | None,
    report: ErrorReport | None,
) -> None:
    """The one terminal transaction: the fenced terminal write, then the kind's hook."""
    report_json: dict[str, Any] | None = None if report is None else report.model_dump(mode="json")

    async def write() -> None:
        async with unit_of_work(head.tenant_id) as session:
            job = await JobRunRepository(session, head.tenant_id).finish(
                job_id,
                attempt,
                status=status,
                result_json=None if outcome is None else outcome.result,
                error_report_json=report_json,
                error_code=(
                    None if report is None or report.error_code is None else report.error_code.value
                ),
            )
            if job is None:
                log.warning("job.stale_write_dropped", job_run_id=str(job_id), attempt=attempt)
                return
            await run_terminal_hook(session, job, outcome)
        log.info("job.finished", job_run_id=str(job_id), status=status, attempt=attempt)

    # a shutdown arriving during the terminal write never leaves it half done
    await asyncio.shield(write())


async def _retry_or_dead_letter(
    head: JobHead,
    job_id: UUID,
    policy: JobPolicy,
    attempt: int,
    max_attempts: int,
    exc: RetryableJobError,
    failure: Any,
) -> None:
    if policy.retry_transport and attempt < max_attempts:
        async with unit_of_work(head.tenant_id) as session:
            released = await JobRunRepository(session, head.tenant_id).release_retry(
                job_id, attempt
            )
        if not released:
            log.warning("job.stale_write_dropped", job_run_id=str(job_id), attempt=attempt)
            return
        delay = backoff_s(attempt, _rand.random())
        log.info("job.retry_scheduled", job_run_id=str(job_id), attempt=attempt, code=exc.code)
        raise RetryLater(delay)
    report = failure(ErrorCode.RETRY_EXHAUSTED, f"retries exhausted after {exc.code.value}")
    await _finish(head, job_id, attempt, "dead_letter", None, report)


async def _release_shutdown(head: JobHead, job_id: UUID, attempt: int) -> None:
    async with unit_of_work(head.tenant_id) as session:
        released = await JobRunRepository(session, head.tenant_id).release_shutdown(job_id, attempt)
    if released:
        log.info("job.released_on_shutdown", job_run_id=str(job_id), attempt=attempt)
    else:
        log.warning("job.stale_write_dropped", job_run_id=str(job_id), attempt=attempt)
