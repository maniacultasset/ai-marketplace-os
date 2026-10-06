"""The per-request context: one `request_id` and, once auth binds it, the `tenant_id`.

Spec [168] (every request has one `request_id`, propagated through contextvars). The request
middleware opens a `request_context()` per HTTP request; S13's worker opens one per job. Log
lines, Sentry events, spans and the `request_id` column defaults all read it from here.

- An inbound `X-Request-ID` is kept only when it is 1-128 characters of `[A-Za-z0-9._:-]` (it fits
  the contract's `RequestId`); anything else is replaced by a fresh UUID4, so a raw header value
  never reaches a response, a log line or a row.
- `tenant_id` is bound only from the authenticated principal (`bind_tenant`), never from a header.
- The context holds a mutable `RequestContext`, so a bind made in a sync dependency (which runs in
  a worker thread on a copy of the context) is visible to the rest of the request.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Final
from uuid import UUID

REQUEST_ID_HEADER: Final = "x-request-id"
REQUEST_ID_PATTERN: Final = r"[A-Za-z0-9._:-]{1,128}"
_REQUEST_ID_RE: Final = re.compile(REQUEST_ID_PATTERN)


@dataclass(slots=True)
class RequestContext:
    request_id: str
    tenant_id: str | None = None


_CURRENT: Final[ContextVar[RequestContext | None]] = ContextVar("mkos_request", default=None)


def new_request_id() -> str:
    return str(uuid.uuid4())


def accept_request_id(raw: str | None) -> str:
    """`raw` when it is a safe id (`REQUEST_ID_PATTERN`, whole string), else a new UUID4."""
    if raw is not None and _REQUEST_ID_RE.fullmatch(raw):
        return raw
    return new_request_id()


@contextmanager
def request_context(request_id: str) -> Iterator[RequestContext]:
    """Bind `request_id` for the block; the outer context (if any) is restored on exit."""
    ctx = RequestContext(request_id=request_id)
    token = _CURRENT.set(ctx)
    try:
        yield ctx
    finally:
        _CURRENT.reset(token)


def current_context() -> RequestContext | None:
    return _CURRENT.get()


def current_request_id() -> str | None:
    ctx = _CURRENT.get()
    return ctx.request_id if ctx is not None else None


def current_tenant_id() -> str | None:
    ctx = _CURRENT.get()
    return ctx.tenant_id if ctx is not None else None


def bind_tenant(tenant_id: UUID | str) -> None:
    """Record the authenticated tenant on the current request. A no-op outside a request.

    Call it only after the token's signature, expiry and membership checks pass."""
    ctx = _CURRENT.get()
    if ctx is not None:
        ctx.tenant_id = str(tenant_id)
