"""The job wrapper (DATABASE.md:1027, :1487; spike REPORT §5 item 3): every queue task runs
`run_job(job_run_id)`.

1. Name the job through `jr_by_id` (`resolve_job`); an unknown id is dropped.
2. Re-bind the origin `request_id` (a fresh one if the row has none) and the tenant.
3. Claim it in one short `unit_of_work`: the compare-and-set `queued → running`, which also needs
   a `trial|active` tenant. 0 rows: a queued job of a stopped tenant is cancelled (and its hook
   runs), anything else is a duplicate or stale delivery and is dropped.
4. Run the body under `asyncio.timeout(policy.timeout_s)` with no transaction open.
5. One short terminal transaction: the body's `persist` (S18: after a fenced `FOR UPDATE` of the
   claimed attempt, so a reaped attempt never writes), the outcome, then the kind's terminal hook.
   Every write is fenced on `status = 'running' AND attempt = <claimed>`; 0 rows (the lease was
   reaped and a newer attempt owns the row) logs `job.stale_write_dropped` and writes nothing else.
   The outcome's `zero_reservation` zeroes the budget reservation in the same UPDATE (K2).
6. If that transaction raises (a persist or hook error), it rolls back whole, the wrapper logs the
   exception class, and a second fenced terminal transaction writes `failed` + `INTERNAL_ERROR`
   (a fixed message, the reservation kept) plus the hook. If that raises too, the row stays
   `running` and the reaper recovers it (S18 CP11).

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
from app.repositories.uow import repositories_on
from app.services.job_hooks import run_terminal_hook
from app.workers import registry
from app.workers.errors import JobFailed, RetryableJobError, RetryLater, system_report
from app.workers.policy import POLICIES, JobPolicy, backoff_s, lease_s
from app.workers.registry import JobContext, Outcome, Persist

log = get_logger(__name__)
_rand = secrets.SystemRandom()


def policy_for(kind: JobKind, policies: Mapping[JobKind, JobPolicy] | None = None) -> JobPolicy:
    return (POLICIES if policies is None else policies)[kind]


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
        claimed = await _claim(job_id, head, policy, ctx.request_id)
        if claimed is not None:
            await _execute(job_id, head, kind, policy, claimed, ctx.request_id)


async def _claim(job_id: UUID, head: JobHead, policy: JobPolicy, request_id: str) -> Claimed | None:
    async with unit_of_work(head.tenant_id) as session:
        repo = JobRunRepository(session, head.tenant_id)
        claimed = await repo.claim(job_id, lease_s(policy), request_id=request_id)
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
        return system_report(
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
        await _finish(
            head,
            job_id,
            attempt,
            "failed",
            None,
            exc.report,
            persist=exc.persist,
            zero_reservation=exc.zero_reservation,
        )
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
    await _finish(
        head,
        job_id,
        attempt,
        outcome.status,
        outcome,
        outcome.error,
        persist=outcome.persist,
        zero_reservation=outcome.zero_reservation,
    )


class _BodyTimeoutError(Exception):
    """A `TimeoutError` the body raised itself (not our deadline): an ordinary failure."""


async def _finish(
    head: JobHead,
    job_id: UUID,
    attempt: int,
    status: str,
    outcome: Outcome | None,
    report: ErrorReport | None,
    *,
    persist: Persist | None = None,
    zero_reservation: bool = False,
) -> None:
    """The one terminal transaction (module docstring, steps 5-6)."""
    report_json: dict[str, Any] | None = None if report is None else report.model_dump(mode="json")
    tenant = head.tenant_id

    async def write() -> None:
        async with unit_of_work(tenant) as session:
            repo = JobRunRepository(session, tenant)
            if persist is not None:
                if await repo.lock_running(job_id, attempt) is None:
                    log.warning("job.stale_write_dropped", job_run_id=str(job_id), attempt=attempt)
                    return
                await persist(repositories_on(session, tenant))
            job = await repo.finish(
                job_id,
                attempt,
                status=status,
                result_json=None if outcome is None else outcome.result,
                error_report_json=report_json,
                error_code=(
                    None if report is None or report.error_code is None else report.error_code.value
                ),
                zero_reservation=zero_reservation,
            )
            if job is None:
                log.warning("job.stale_write_dropped", job_run_id=str(job_id), attempt=attempt)
                return
            await run_terminal_hook(session, job, outcome)
        log.info("job.finished", job_run_id=str(job_id), status=status, attempt=attempt)

    # a shutdown arriving during the terminal write never leaves it half done
    try:
        await asyncio.shield(write())
    except Exception as exc:  # the terminal transaction rolled back whole
        log.error(
            "job.persist_error",
            job_run_id=str(job_id),
            tenant_id=str(tenant),
            kind=head.kind,
            error_class=type(exc).__name__,
        )
        await asyncio.shield(_fallback_finish(head, job_id, attempt))


def fallback_report(
    kind: str, attempt: int, max_attempts: int, request_id: str | None
) -> ErrorReport:
    """The fixed report of a terminal write that failed (CP11): no exception text."""
    generate = kind == JobKind.LISTING_GENERATE.value
    item = ErrorItem(
        field="listing" if generate else "request",
        error_code=ErrorCode.INTERNAL_ERROR,
        message="generation failed" if generate else "the job's terminal write failed",
        source=ErrorSource.SYSTEM,
        retryable=False,
    )
    return ErrorReport(
        status="failed",
        error_code=ErrorCode.INTERNAL_ERROR,
        retryable=False,
        attempts_used=min(attempt, 3),
        max_attempts=max(1, min(max_attempts, 3)),
        errors=[item],
        request_id=request_id,
    )


async def _fallback_finish(head: JobHead, job_id: UUID, attempt: int) -> None:
    """`failed` + `INTERNAL_ERROR` and the kind's hook, the reservation kept. A failure here is
    logged and left to the reaper (the row stays `running`)."""
    tenant = head.tenant_id
    try:
        async with unit_of_work(tenant) as session:
            repo = JobRunRepository(session, tenant)
            current = await repo.lock_running(job_id, attempt)
            if current is None:
                log.warning("job.stale_write_dropped", job_run_id=str(job_id), attempt=attempt)
                return
            report = fallback_report(
                head.kind, attempt, current.max_attempts, current.request_id or head.request_id
            )
            job = await repo.finish(
                job_id,
                attempt,
                status="failed",
                error_report_json=report.model_dump(mode="json"),
                error_code=ErrorCode.INTERNAL_ERROR.value,
            )
            if job is None:
                return
            await run_terminal_hook(session, job, None)
        log.info("job.finished", job_run_id=str(job_id), status="failed", attempt=attempt)
    except Exception as exc:  # recorded; the reaper recovers the running row
        log.error(
            "job.fallback_failed",
            job_run_id=str(job_id),
            tenant_id=str(tenant),
            error_class=type(exc).__name__,
        )


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
