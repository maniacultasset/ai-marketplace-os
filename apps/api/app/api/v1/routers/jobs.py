"""Jobs (owner step S13): the job envelope, read from `job_runs`, and the cancel of a queued job.
Dead letters are staff tooling (`python -m app.cli jobs ...`), not tenant routes (T82)."""

from __future__ import annotations

from typing import Annotated, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi import Path as PathParam
from mkos_schemas.codes import ErrorCode
from mkos_schemas.jobs import JobEnvelope

from app.api.errors import ApiError, NotImplementedYet
from app.api.v1.deps import IdempotencyKey, get_current_principal
from app.api.v1.surface import OPS, route, stub
from app.core.rbac import Principal
from app.services import jobs

router = APIRouter()
JobId = Annotated[UUID, PathParam()]
CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]


@route(router, OPS["jobs_get"])
async def jobs_get(job_id: JobId, principal: CurrentPrincipal) -> JobEnvelope:
    """The job envelope, read from `job_runs`, never from the queue."""
    found = await jobs.get(principal, job_id)
    if found is None:
        raise ApiError(404, ErrorCode.NOT_FOUND)
    return found


@route(router, OPS["jobs_cancel"])
@stub
async def jobs_cancel(job_id: JobId, idempotency_key: IdempotencyKey) -> NoReturn:
    """Cancel a queued job; a finished one is 409 INVALID_STATUS_TRANSITION."""
    raise NotImplementedYet("jobs_cancel")
