"""The ASGI app: `uvicorn app.main:app`, one process per instance (ADR-0005)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import health
from app.api.v1.router import router as v1_router
from app.core.config import get_settings
from app.db.session import dispose_engine
from app.services.health import ReadinessCheck


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    # Production serves no docs: Swagger UI loads third-party CDN assets on the API origin.
    # app.openapi() still works, so the committed openapi.json export is unaffected.
    serve_docs = settings.app_env != "production"
    app = FastAPI(
        title="AI Marketplace OS API",
        version="0.0.0",
        docs_url="/docs" if serve_docs else None,
        openapi_url="/openapi.json" if serve_docs else None,
        redoc_url=None,
        swagger_ui_oauth2_redirect_url=None,
        # Behind Cloud Run a slash redirect would point at http://; a mismatch is a plain 404.
        redirect_slashes=False,
        lifespan=lifespan,
    )
    app.state.readiness = ReadinessCheck()
    app.include_router(health.router)
    app.include_router(v1_router, prefix=settings.api_v1_prefix)
    return app


app = create_app()
