"""Jobs (CANON.md:60; S13 A13-A14): the job envelope, read from `job_runs` only (never the queue),
and the cancel of a queued job.

- `to_envelope(job)`: `JobEnvelope` from the row. `progress_pct` and the import's
  `result_url` come from `result_json` (`{"import_id", "progress_pct"}`, Q8); a finished
  `succeeded`/`partial` job reads 100. `stage` is the row's `node`. A row with no `request_id`
  (written outside a request) shows its `correlation_id`, so the field is never empty.
- `get(principal, job_id)`: the caller's tenant's job, or None (another tenant's reads as missing).
- `cancel(principal, job_id, key, path)`: `queued → cancelled` and the queue entry cancelled in
  one idempotent transaction, then the kind's terminal hook; any other status is 409
  `INVALID_STATUS_TRANSITION` (a running job is not cancellable, BACKEND-ARCHITECTURE:1718).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import JobKind, JobState, PipelineStage
from mkos_schemas.errors import ErrorReport
from mkos_schemas.jobs import JobEnvelope

from app.core.errors import ConflictError, NotFoundError
from app.core.rbac import Principal
from app.db.models.ops import JobRun
from app.repositories.jobs import JobRunRepository, cancel_queue_entry
from app.repositories.transactions import unit_of_work
from app.services.idempotency import idempotent
from app.services.job_hooks import run_terminal_hook

API_V1 = "/api/v1"
DONE = frozenset({JobState.SUCCEEDED, JobState.PARTIAL})


def _progress(job: JobRun, status: JobState) -> int:
    result = job.result_json or {}
    value = result.get("progress_pct")
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 100:
        return 100 if status in DONE else value
    return 100 if status in DONE else 0


PUBLISH_KINDS = frozenset({JobKind.LISTING_PUBLISH.value, JobKind.LISTING_STATUS_SYNC.value})


def _result_url(job: JobRun) -> str | None:
    if job.kind in PUBLISH_KINDS and job.listing_id is not None:
        # S21: a publish or sync job's per-field results are the listing's submissions
        return f"{API_V1}/listings/{job.listing_id}/submissions"
    import_id = (job.result_json or {}).get("import_id")
    if isinstance(import_id, str):
        try:
            return f"{API_V1}/imports/{UUID(import_id)}"
        except ValueError:
            return None
    return None


def to_envelope(job: JobRun) -> JobEnvelope:
    status = JobState(job.status)
    return JobEnvelope(
        job_id=job.id,
        kind=JobKind(job.kind),
        status=status,
        stage=PipelineStage(job.node) if job.node else None,
        progress_pct=_progress(job, status),
        attempt=job.attempt,
        max_attempts=job.max_attempts,
        listing_id=job.listing_id,
        result_url=_result_url(job),
        error=(
            ErrorReport.model_validate(job.error_report_json)
            if job.error_report_json is not None
            else None
        ),
        request_id=job.request_id or str(job.correlation_id),
        queued_at=job.queued_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


async def get(principal: Principal, job_id: UUID) -> JobEnvelope | None:
    async with unit_of_work(principal.tenant_id, user_id=principal.user_id) as session:
        job = await JobRunRepository(session, principal.tenant_id).get_by_id(job_id)
        return None if job is None else to_envelope(job)


async def cancel(principal: Principal, job_id: UUID, key: str, path: str) -> JobEnvelope:
    """Cancel a queued job under the Idempotency-Key; the replay returns the first response."""

    async def handler(session: Any) -> tuple[int, dict[str, Any]]:
        repo = JobRunRepository(session, principal.tenant_id)
        job = await repo.cancel_queued(job_id)
        if job is None:
            if await repo.get_by_id(job_id) is None:
                raise NotFoundError("job")
            raise ConflictError(ErrorCode.INVALID_STATUS_TRANSITION, "job_runs_status")
        await cancel_queue_entry(session, job)
        await run_terminal_hook(session, job, None)
        return 200, to_envelope(job).model_dump(mode="json")

    _status, body = await idempotent(principal, key, "POST", path, b"", handler)
    return JobEnvelope.model_validate(body)
