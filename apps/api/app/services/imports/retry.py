"""`POST /api/v1/imports/{import_id}/rows/retry` (S13 B14; PD10, PD13, Q10, Q11; DATABASE.md:1481).

One `idempotent` transaction: the tenant import lock first (so two concurrent retries of one
import, or a retry and an upload, count one after the other), then the import (another tenant's
reads as missing), a live job on it or no failed rows → 409 `INVALID_STATUS_TRANSITION`, the
live-import cap → 429, and else a new `bulk_import` job (`payload.retry`, `parent_job_id` = the
import's current job, which a staff requeue keeps pointing at the newest job; `http_key=None`,
Q11) that takes the import over (`job_id`, `status='queued'`). The body re-runs only the
`failed` rows, from their stored cells.
"""

from __future__ import annotations

import uuid
from typing import Any, Final

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import JobKind

from app.core.errors import ConflictError, NotFoundError
from app.core.rbac import Principal
from app.repositories.imports import LIVE_JOB_STATES, ImportRepository, ImportRowRepository
from app.repositories.jobs import EnqueueSpec, JobRunRepository
from app.services.idempotency import idempotent
from app.services.imports.cap import enforce_live_cap

API_V1: Final = "/api/v1"


def _not_retryable() -> ConflictError:
    return ConflictError(ErrorCode.INVALID_STATUS_TRANSITION, "imports_rows_retry")


async def retry_failed_rows(
    principal: Principal, import_id: uuid.UUID, key: str, path: str
) -> dict[str, Any]:
    tenant = principal.tenant_id

    async def handler(session: Any) -> tuple[int, dict[str, Any]]:
        imports = ImportRepository(session, tenant)
        await imports.lock_tenant_imports()
        imp = await imports.get(import_id)
        if imp is None:
            raise NotFoundError("import_id")
        jobs = JobRunRepository(session, tenant)
        current = await jobs.get_by_id(imp.job_id)
        if current is None or current.status in LIVE_JOB_STATES:
            raise _not_retryable()
        if not await ImportRowRepository(session, tenant).count_status(imp.id, "failed"):
            raise _not_retryable()
        await enforce_live_cap(imports)
        enqueued = await jobs.enqueue(
            EnqueueSpec(
                kind=JobKind.BULK_IMPORT,
                entity_type="import",
                entity_id=imp.id,
                created_by=principal.user_id,
                payload={"retry": True},
                parent_job_id=current.id,
            )
        )
        if not await imports.hand_to_job(imp.id, current.id, enqueued.job.id):
            raise _not_retryable()
        body = {
            "job_id": str(enqueued.job.id),
            "listing_id": None,
            "status": "queued",
            "estimated_duration_seconds": 30,
            "poll_url": f"{API_V1}/jobs/{enqueued.job.id}",
        }
        return 202, body

    _status, body = await idempotent(principal, key, "POST", path, b"", handler)
    return body
