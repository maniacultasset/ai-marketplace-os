"""Unversioned probes (CANON.md:34). Routers import services only, never `sqlalchemy` or
`app.db` (BACKEND-ARCHITECTURE.md:1273; .importlinter)."""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.services.health import ReadinessCheck

router = APIRouter()


def readiness_from(request: Request) -> ReadinessCheck:
    return cast(ReadinessCheck, request.app.state.readiness)


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness: always 200 and never touches the database."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    """Readiness: 200 when `SELECT 1` succeeds, else 503 with a fixed body and no detail."""
    if await readiness_from(request).is_ready():
        return JSONResponse({"status": "ok"})
    return JSONResponse({"status": "unavailable"}, status_code=503)
