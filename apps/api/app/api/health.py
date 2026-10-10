"""Unversioned probes (CANON.md:34). Routers import services only, never `sqlalchemy` or
`app.db` (BACKEND-ARCHITECTURE.md:1273; .importlinter)."""

from __future__ import annotations

from typing import Final, cast

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.api.v1.deps import public
from app.services.health import ReadinessCheck

# Public and never rate limited: a liveness probe must not depend on the database (CODE-PLAN S10 C6).
router = APIRouter(dependencies=[public(rate_limited=False)])


def readiness_from(request: Request) -> ReadinessCheck:
    return cast(ReadinessCheck, request.app.state.readiness)


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness: always 200 and never touches the database."""
    return {"status": "ok"}


_NOT_READY: Final = {"status": "unavailable", "checks": {"postgres": "unavailable"}}


@router.get(
    "/readyz",
    responses={
        503: {
            "description": "Not ready: `SELECT 1` failed. The body names the dependency only.",
            "content": {"application/json": {"example": _NOT_READY}},
        }
    },
)
async def readyz(request: Request) -> JSONResponse:
    """Readiness: 200 when `SELECT 1` succeeds, else 503 with a fixed body and no detail."""
    # The docstring is the committed openapi.json description: keep it. The body names the
    # dependency (Postgres) and its state only, never the driver's error.
    if await readiness_from(request).is_ready():
        return JSONResponse({"status": "ok", "checks": {"postgres": "ok"}})
    return JSONResponse(_NOT_READY, status_code=503)
