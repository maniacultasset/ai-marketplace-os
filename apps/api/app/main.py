"""The ASGI app: `uvicorn app.main:app`, one process per instance (ADR-0005)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib import metadata

from fastapi import FastAPI

from app.api import health
from app.api.body_limit import BodyLimitMiddleware
from app.api.errors import install_error_handlers
from app.api.v1.router import router as v1_router
from app.core.asgi import MkosApp
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import dispose_engine
from app.services.health import ReadinessCheck


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings)
    # Production serves no docs: Swagger UI loads third-party CDN assets on the API origin.
    # app.openapi() still works, so the committed openapi.json export is unaffected.
    serve_docs = settings.app_env != "production"
    app = MkosApp(
        title="AI Marketplace OS API",
        # The contract version: openapi.json and @mkos/schemas share mkos-schemas' version.
        version=metadata.version("mkos-schemas"),
        docs_url="/docs" if serve_docs else None,
        openapi_url="/openapi.json" if serve_docs else None,
        redoc_url=None,
        swagger_ui_oauth2_redirect_url=None,
        # Behind Cloud Run a slash redirect would point at http://; a mismatch is a plain 404.
        redirect_slashes=False,
        lifespan=lifespan,
        # One component per model (no `-Input`/`-Output` pairs), so FE type names stay plain.
        separate_input_output_schemas=False,
    )
    app.state.readiness = ReadinessCheck()
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(v1_router, prefix=settings.api_v1_prefix)
    # Added last, so it is the outermost user middleware: the cap holds before any body parsing
    # or auth dependency runs (CANON.md:83; S3 AC16).
    app.add_middleware(BodyLimitMiddleware, max_bytes=settings.request_max_body_bytes)
    return app


app = create_app()
