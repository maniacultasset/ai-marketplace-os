"""Jobs (owner step S13): the job envelope, read from `job_runs`, and the cancel of a queued job.
Dead letters are staff tooling (`python -m app.cli jobs ...`), not tenant routes (T82)."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi import Path as PathParam
from mkos_schemas.codes import ErrorCode
from mkos_schemas.jobs import JobEnvelope

from app.api.errors import ApiError
from app.api.v1.deps import IdempotencyKey, get_current_principal
from app.api.v1.surface import OPS, route
from app.core.errors import ConflictError, NotFoundError
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
async def jobs_cancel(
    job_id: JobId,
    idempotency_key: IdempotencyKey,
    principal: CurrentPrincipal,
    request: Request,
) -> JobEnvelope:
    """Cancel a queued job; a finished one is 409 INVALID_STATUS_TRANSITION."""
    try:
        return await jobs.cancel(principal, job_id, idempotency_key, request.url.path)
    except NotFoundError:
        raise ApiError(404, ErrorCode.NOT_FOUND) from None
    except ConflictError as exc:
        if exc.code not in (ErrorCode.INVALID_STATUS_TRANSITION, ErrorCode.IDEMPOTENCY_KEY_REUSED):
            raise
        raise ApiError(409, exc.code) from None
