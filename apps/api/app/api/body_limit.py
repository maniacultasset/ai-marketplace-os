"""The request body cap (CANON.md:83; ENV.md REQUEST_MAX_BODY_BYTES; S3 AC16).

A pure ASGI middleware installed outermost in `create_app()`, so it runs before FastAPI parses any
form or JSON body and before any auth dependency (FastAPI reads the body first,
`fastapi/routing.py:425-469`). It refuses:
- a declared `Content-Length` over the cap: 413 `PAYLOAD_TOO_LARGE` before the app runs;
- a non-numeric, signed or repeated `Content-Length`: 400 `VALIDATION_FAILED`;
- a stream (chunked, or a `Content-Length` that lies) that passes the cap: it stops reading and
  answers the same 413, replacing whatever the app would have sent, if no response has started.

`path_caps` (S14b T12, C7) tightens the cap for exact request paths: the effective cap of a path
is `min(max_bytes, path_caps[path])`. Every other path keeps `max_bytes`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from mkos_schemas.codes import ErrorCode
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.errors import error_response_for_scope

_DIGITS: Final = frozenset(b"0123456789")


class _BodyTooLarge(Exception):  # noqa: N818 - a control-flow signal, never shown
    """Raised from `receive` once the streamed bytes pass the cap."""


def _content_length(scope: Scope) -> tuple[bool, int | None]:
    """(well_formed, value). No header is well formed with value None."""
    values = [v for k, v in scope.get("headers", ()) if k.lower() == b"content-length"]
    if not values:
        return True, None
    if len(values) > 1:
        return False, None
    raw = values[0].strip()
    if not raw or any(b not in _DIGITS for b in raw):
        return False, None
    return True, int(raw)


class BodyLimitMiddleware:
    def __init__(
        self, app: ASGIApp, max_bytes: int, path_caps: Mapping[str, int] | None = None
    ) -> None:
        if max_bytes < 0:
            raise ValueError("max_bytes must be >= 0")
        caps = dict(path_caps or {})
        if any(cap < 0 for cap in caps.values()):
            raise ValueError("path caps must be >= 0")
        self.app = app
        self.max_bytes = max_bytes
        self.path_caps = caps

    def cap_for(self, path: str) -> int:
        cap = self.path_caps.get(path)
        return self.max_bytes if cap is None else min(self.max_bytes, cap)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        max_bytes = self.cap_for(scope.get("path", ""))
        well_formed, declared = _content_length(scope)
        if not well_formed:
            await error_response_for_scope(scope, 400, ErrorCode.VALIDATION_FAILED)(
                scope, receive, send
            )
            return
        if declared is not None and declared > max_bytes:
            await error_response_for_scope(scope, 413, ErrorCode.PAYLOAD_TOO_LARGE)(
                scope, receive, send
            )
            return

        received = 0
        too_large = False
        started = False

        async def limited_receive() -> Message:
            nonlocal received, too_large
            if too_large:
                raise _BodyTooLarge
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > max_bytes:
                    too_large = True
                    raise _BodyTooLarge
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal started
            if too_large and not started:
                return  # the app's own answer to the failed read is replaced by the 413 below
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except _BodyTooLarge:
            if started:
                raise
        if too_large and not started:
            await error_response_for_scope(scope, 413, ErrorCode.PAYLOAD_TOO_LARGE)(
                scope, receive, send
            )
