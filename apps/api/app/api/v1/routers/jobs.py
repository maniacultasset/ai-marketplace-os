"""Job stubs (owner step S13). Dead letters are staff tooling, not tenant routes (T82)."""

from __future__ import annotations

from typing import Annotated, NoReturn
from uuid import UUID

from fastapi import APIRouter
from fastapi import Path as PathParam

from app.api.errors import NotImplementedYet
from app.api.v1.deps import IdempotencyKey
from app.api.v1.surface import OPS, route, stub

router = APIRouter()
JobId = Annotated[UUID, PathParam()]


@route(router, OPS["jobs_get"])
@stub
async def jobs_get(job_id: JobId) -> NoReturn:
    """The job envelope, read from `job_runs`, never from the queue."""
    raise NotImplementedYet("jobs_get")


@route(router, OPS["jobs_cancel"])
@stub
async def jobs_cancel(job_id: JobId, idempotency_key: IdempotencyKey) -> NoReturn:
    """Cancel a queued job; a finished one is 409 INVALID_STATUS_TRANSITION."""
    raise NotImplementedYet("jobs_cancel")
