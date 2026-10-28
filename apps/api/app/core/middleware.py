"""The outermost request middleware (pure ASGI): request id, access line, server span, and the one
log line per unhandled exception.

`MkosApp.build_middleware_stack()` wraps the whole Starlette stack in it, outside
`ServerErrorMiddleware`, so 404, 405, 422 and 500 answers all pass through it.

Per HTTP request:
- `X-Request-ID` is kept only when it is one safe id (`accept_request_id`), else a UUID4 is minted;
  the id is bound for the request (`request_context`) and set as the only `X-Request-ID` on the
  response (any inner one is dropped).
- One `app.access` INFO line: method, route template (`<unmatched>` when no route matched),
  status and `duration_ms`. No query string, client IP or user agent. Never for the probes.
- One SERVER span (never for the probes), always a trace root: inbound trace headers are removed
  before the app runs (`strip_trace_headers`), so a caller can neither force sampling nor splice
  its spans into another trace.
- An exception that escapes the app is logged once at ERROR, with the request id and a redacted
  traceback, and is not re-raised: the 500 has been sent (by Starlette's error middleware, or here
  if no response had started), and a re-raise would only add uvicorn's id-less traceback line.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import nullcontext
from typing import Final

from opentelemetry.trace import Span, SpanKind, Status, StatusCode, Tracer, use_span
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.logging import get_logger
from app.core.redaction import redact_text
from app.core.request_context import REQUEST_ID_HEADER, accept_request_id, request_context

PROBE_PATHS: Final = frozenset({"/healthz", "/readyz"})
UNMATCHED_ROUTE: Final = "<unmatched>"
TRACE_HEADERS: Final = frozenset({b"traceparent", b"tracestate", b"sentry-trace", b"baggage"})
_REQUEST_ID_HEADER_BYTES: Final = REQUEST_ID_HEADER.encode("latin-1")

_access_log = get_logger("app.access")
_error_log = get_logger("app.request")

ScopeHook = Callable[[str], None]


def strip_trace_headers(scope: Scope) -> Scope:
    """A shallow copy of an `http` scope without the inbound trace-context headers."""
    if scope.get("type") != "http":
        return scope
    headers = scope.get("headers") or ()
    kept = [(k, v) for k, v in headers if k.lower() not in TRACE_HEADERS]
    if len(kept) == len(headers):
        return scope
    return {**scope, "headers": kept}


def _inbound_request_id(scope: Scope) -> str | None:
    values = [v for k, v in scope.get("headers") or () if k.lower() == _REQUEST_ID_HEADER_BYTES]
    if len(values) != 1:
        return None
    raw = values[0]
    return raw.decode("latin-1") if isinstance(raw, bytes) else None


def _xff_entries(scope: Scope) -> int:
    """How many non-blank entries every X-Forwarded-For header holds (DPP-2: measures the proxy
    hops in front of the app; the same parse as `app.api.rate_limit.client_ip`). Never a value."""
    count = 0
    for name, value in scope.get("headers") or ():
        if name.lower() == b"x-forwarded-for":
            count += sum(1 for entry in value.decode("latin-1").split(",") if entry.strip())
    return count


def _route_template(scope: Scope) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) and path else UNMATCHED_ROUTE


async def _send_500(scope: Scope, receive: Receive, send: Send) -> None:
    # Imported here: the error envelope lives in the API layer, which imports this package.
    from app.api.errors import error_response_for_scope

    await error_response_for_scope(scope, 500)(scope, receive, send)


class _IdSender:
    """The `send` wrapper: sets our `X-Request-ID` as the only one and records the status."""

    __slots__ = ("header_value", "send", "started", "status")

    def __init__(self, send: Send, request_id: str) -> None:
        self.send = send
        self.header_value = request_id.encode("latin-1")
        self.started = False
        self.status = 500

    async def __call__(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            self.started = True
            self.status = int(message["status"])
            headers = [
                (k, v)
                for k, v in message.get("headers") or ()
                if k.lower() != _REQUEST_ID_HEADER_BYTES
            ]
            headers.append((_REQUEST_ID_HEADER_BYTES, self.header_value))
            message = {**message, "headers": headers}
        await self.send(message)


class RequestContextMiddleware:
    def __init__(
        self, app: ASGIApp, *, tracer: Tracer, on_request: ScopeHook | None = None
    ) -> None:
        self.app = app
        self.tracer = tracer
        self.on_request = on_request

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = accept_request_id(_inbound_request_id(scope))
        probe = scope.get("path") in PROBE_PATHS
        method = str(scope.get("method", ""))
        sender = _IdSender(send, request_id)
        with request_context(request_id):
            span = None if probe else self.tracer.start_span(method, kind=SpanKind.SERVER)
            current = (
                nullcontext()
                if span is None
                else use_span(
                    span,
                    end_on_exit=False,
                    record_exception=False,
                    set_status_on_exception=False,
                )
            )
            with current:
                # S23: inside the SERVER span, so the hook can tag its trace id too
                if self.on_request is not None:
                    self.on_request(request_id)
                await self._run(scope, receive, sender, span, request_id, probe, method)

    async def _run(
        self,
        scope: Scope,
        receive: Receive,
        send: _IdSender,
        span: Span | None,
        request_id: str,
        probe: bool,
        method: str,
    ) -> None:
        clock = time.perf_counter()
        failure: Exception | None = None
        try:
            await self.app(scope, receive, send)
        except Exception as exc:  # logged once below (C4), never silently
            failure = exc
            if not send.started:
                await _send_500(scope, receive, send)
        finally:
            status = 500 if failure is not None else send.status
            duration_ms = round((time.perf_counter() - clock) * 1000, 3)
            route = _route_template(scope)
            if failure is not None:
                _error_log.error(
                    "unhandled exception",
                    method=method,
                    route=route,
                    exc_info=(type(failure), failure, failure.__traceback__),
                )
            if not probe:
                _access_log.info(
                    "request",
                    method=method,
                    route=route,
                    status=status,
                    duration_ms=duration_ms,
                    xff_entries=_xff_entries(scope),
                )
            if span is not None:
                _finish_span(span, method, route, status, request_id, failure)


def _finish_span(
    span: Span, method: str, route: str, status: int, request_id: str, failure: Exception | None
) -> None:
    span.update_name(f"{method} {route}")
    span.set_attribute("http.request.method", method)
    span.set_attribute("http.route", route)
    span.set_attribute("http.response.status_code", status)
    span.set_attribute("request_id", request_id)
    if status >= 500:
        if failure is not None:
            # Type and redacted message only: `record_exception` would store the raw text and
            # the traceback.
            span.add_event(
                "exception",
                {
                    "exception.type": type(failure).__qualname__,
                    "exception.message": redact_text(str(failure)),
                    "exception.escaped": True,
                },
            )
        span.set_status(Status(StatusCode.ERROR))
    span.end()
