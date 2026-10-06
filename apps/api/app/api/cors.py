"""CORS for the FE origins (ENV.md ALLOWED_ORIGINS; CODE-PLAN S10 C13).

Bearer tokens need no cookies, so credentials are off. The allowed request headers are the ones
the API reads; `X-Request-ID` is exposed so the FE can quote it. A preflight from an origin that
is not allowed is answered in the one error envelope (invariant 8), not Starlette's plain text.
"""

from __future__ import annotations

from typing import Final

from mkos_schemas.codes import ErrorCode
from starlette.datastructures import Headers
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from app.api.errors import STATUS_MESSAGES, envelope_body
from app.core.request_context import current_request_id, new_request_id

ALLOWED_METHODS: Final = ("GET", "POST", "PATCH")
ALLOWED_HEADERS: Final = (
    "Authorization",
    "Content-Type",
    "Idempotency-Key",
    "If-Match",
    "X-Request-ID",
)
EXPOSED_HEADERS: Final = ("X-Request-ID",)
PREFLIGHT_MAX_AGE: Final = 600


def parse_origins(raw: str) -> list[str]:
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


class ApiCORSMiddleware(CORSMiddleware):
    def __init__(self, app: ASGIApp, *, allowed_origins: str) -> None:
        super().__init__(
            app,
            allow_origins=parse_origins(allowed_origins),
            allow_methods=list(ALLOWED_METHODS),
            allow_headers=list(ALLOWED_HEADERS),
            allow_credentials=False,
            expose_headers=list(EXPOSED_HEADERS),
            max_age=PREFLIGHT_MAX_AGE,
        )

    def preflight_response(self, request_headers: Headers) -> Response:
        response = super().preflight_response(request_headers)
        if response.status_code < 400:
            return response
        # Disallowed origin, method or header: the envelope, and no CORS grant at all.
        correlation = current_request_id() or new_request_id()
        body = envelope_body(400, ErrorCode.VALIDATION_FAILED, STATUS_MESSAGES[400], correlation)
        return JSONResponse(body, status_code=400)
