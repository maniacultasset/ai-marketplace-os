"""`MkosApp`: FastAPI with the request middleware wrapped around the whole stack.

The wrap sits outside Starlette's `ServerErrorMiddleware`, so every answer (404, 405, 422, 500)
carries the request id. Kept apart from `app.main`, which builds the app at import.
"""

from __future__ import annotations

from fastapi import FastAPI
from opentelemetry.trace import NoOpTracer, Tracer
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.middleware import RequestContextMiddleware, ScopeHook, strip_trace_headers


class MkosApp(FastAPI):
    tracer: Tracer = NoOpTracer()
    on_request: ScopeHook | None = None

    def build_middleware_stack(self) -> ASGIApp:
        return RequestContextMiddleware(
            super().build_middleware_stack(), tracer=self.tracer, on_request=self.on_request
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Inbound trace headers come from untrusted callers: dropped before anything (Sentry's
        # patched Starlette included) can continue a trace from them. Every request starts one.
        await super().__call__(strip_trace_headers(scope), receive, send)
