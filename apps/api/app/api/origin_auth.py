"""The origin guard (ADR-0025): only the Cloudflare Worker may reach the API.

The Worker adds `x-kuinn-origin-auth: <EDGE_ORIGIN_SECRET>` to every request it forwards. The
check is split in two, because Sentry patches Starlette's `__call__` and does not scrub custom
headers:
- `OriginGuard.screen(scope)` runs in `MkosApp.__call__`, before anything else sees the scope. It
  removes every `x-kuinn-origin-auth` header (in both modes, so the value never reaches a log, a
  span or Sentry) and stores the verdict in `scope["mkos.origin_ok"]`. A scope key, not a header,
  so no caller can forge it. Without a matched secret it also removes every `x-kuinn-client-ip`,
  so `app.api.rate_limit.edge_client_ip` trusts that header only from the Worker.
- `OriginGuard.wrap(app)` sits inside the request-id and security-header middleware and outside
  Starlette's stack. Under `ORIGIN_AUTH=enforce` it answers 403 `PERMISSION_DENIED` to any scope
  whose verdict is not `True`, outside the health probes, before routing, rate limiting, the body
  cap or any body read. Under `off` it is a pass-through.

Exactly one header instance must match; each secret is compared with `hmac.compare_digest`, all of
them every time (no early exit).
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Final

from mkos_schemas.codes import ErrorCode
from starlette.types import ASGIApp, Receive, Scope, Send

from app.api.errors import error_response_for_scope
from app.core.logging import get_logger
from app.core.middleware import PROBE_PATHS

HEADER: Final = b"x-kuinn-origin-auth"
CLIENT_IP_HEADER: Final = b"x-kuinn-client-ip"
EXEMPT_PATHS: Final = PROBE_PATHS
SCOPE_KEY: Final = "mkos.origin_ok"
_SCREENED_TYPES: Final = frozenset({"http", "websocket"})
_MAX_LOGGED_PATH: Final = 256
_POLICY_VIOLATION: Final = 1008  # the WebSocket close code for a refused connection

_log = get_logger(__name__)


def matches(value: bytes, secrets: tuple[bytes, ...]) -> bool:
    """`compare_digest` against every secret, no early exit; False for no secrets."""
    found = False
    for secret in secrets:
        found |= hmac.compare_digest(value, secret)
    return found


@dataclass(frozen=True, slots=True)
class OriginGuard:
    """The guard's settings: `create_app()` builds one from ORIGIN_AUTH and EDGE_ORIGIN_SECRET."""

    enforce: bool
    secrets: tuple[bytes, ...]

    def screen(self, scope: Scope) -> Scope:
        """A copy of an `http` or `websocket` scope without the origin header, carrying the
        verdict; the edge client-IP header survives only with a matched secret."""
        if scope.get("type") not in _SCREENED_TYPES:
            return scope
        headers = scope.get("headers") or ()
        sent = [value for name, value in headers if name.lower() == HEADER]
        ok = len(sent) == 1 and matches(sent[0], self.secrets)
        client_ips = sum(1 for name, _ in headers if name.lower() == CLIENT_IP_HEADER)
        keep_client_ip = ok and client_ips == 1
        kept = [
            (name, value)
            for name, value in headers
            if name.lower() != HEADER and (keep_client_ip or name.lower() != CLIENT_IP_HEADER)
        ]
        return {**scope, "headers": kept, SCOPE_KEY: ok}

    def wrap(self, app: ASGIApp) -> ASGIApp:
        return OriginGuardMiddleware(app) if self.enforce else app


class OriginGuardMiddleware:
    """`ORIGIN_AUTH=enforce`: a scope `OriginGuard.screen` did not vouch for gets the 403."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope["type"]
        if kind not in _SCREENED_TYPES or scope.get(SCOPE_KEY) is True:
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path in EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return
        _log.warning("origin_auth.denied", path=str(path)[:_MAX_LOGGED_PATH])
        if kind == "websocket":
            await send({"type": "websocket.close", "code": _POLICY_VIOLATION})
            return
        response = error_response_for_scope(scope, 403, ErrorCode.PERMISSION_DENIED)
        await response(scope, receive, send)
