"""`MkosApp`: FastAPI with the request middleware wrapped around the whole stack.

The wrap sits outside Starlette's `ServerErrorMiddleware`, so every answer (404, 405, 422, 500)
carries the request id. Kept apart from `app.main`, which builds the app at import.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import FastAPI
from opentelemetry.trace import NoOpTracer, Tracer
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.middleware import RequestContextMiddleware, ScopeHook, strip_trace_headers

if TYPE_CHECKING:
    from app.api.origin_auth import OriginGuard
    from app.api.security_headers import SecurityHeaders


class MkosApp(FastAPI):
    tracer: Tracer = NoOpTracer()
    on_request: ScopeHook | None = None
    security_headers: SecurityHeaders | None = None
    origin_guard: OriginGuard | None = None

    def build_middleware_stack(self) -> ASGIApp:
        stack = super().build_middleware_stack()
        # ADR-0025: inside the request id and security headers (the 403 carries both), outside
        # Starlette's stack (it answers before routing, rate limits, the body cap and any read).
        if self.origin_guard is not None:
            stack = self.origin_guard.wrap(stack)
        if self.security_headers is not None:
            stack = self.security_headers.wrap(stack)
        return RequestContextMiddleware(stack, tracer=self.tracer, on_request=self.on_request)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Inbound trace headers come from untrusted callers: dropped before anything (Sentry's
        # patched Starlette included) can continue a trace from them. Every request starts one.
        scope = strip_trace_headers(scope)
        # ADR-0025: the origin secret header is removed here, before Sentry's patched layer sees
        # the scope, and the verdict rides on the scope (`OriginGuard.screen`).
        if self.origin_guard is not None:
            scope = self.origin_guard.screen(scope)
        await super().__call__(scope, receive, send)
