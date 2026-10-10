"""Security response headers (S26 T8; OWASP API8).

A pure ASGI middleware. `create_app()` sets `MkosApp.security_headers`, and the app wraps it
outside Starlette's `ServerErrorMiddleware`, so every answer (a route, an error envelope, a 404,
a 413 from the body cap, an unhandled 500) carries:
- `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer` on every response;
- `Cache-Control: no-store` on `/api/v1/*` (tenant data and tokens never sit in a shared cache);
- `Strict-Transport-Security` outside `APP_ENV=local` (staging and production are HTTPS only).

A header the app already set is kept (the admin gate and the OAuth callback set their own).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

ALWAYS: Final = (
    ("x-content-type-options", "nosniff"),
    ("referrer-policy", "no-referrer"),
)
NO_STORE: Final = ("cache-control", "no-store")
HSTS: Final = ("strict-transport-security", "max-age=31536000; includeSubDomains")


def headers_for(path: str, *, api_prefix: str, hsts: bool) -> tuple[tuple[str, str], ...]:
    """The headers a response to `path` gets (before the app's own ones win)."""
    out = list(ALWAYS)
    if path == api_prefix or path.startswith(api_prefix + "/"):
        out.append(NO_STORE)
    if hsts:
        out.append(HSTS)
    return tuple(out)


@dataclass(frozen=True, slots=True)
class SecurityHeaders:
    """The middleware's settings; `wrap(app)` builds it."""

    api_prefix: str
    hsts: bool

    def wrap(self, app: ASGIApp) -> SecurityHeadersMiddleware:
        return SecurityHeadersMiddleware(app, api_prefix=self.api_prefix, hsts=self.hsts)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, *, api_prefix: str, hsts: bool) -> None:
        self.app = app
        self.api_prefix = api_prefix.rstrip("/")
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        extra = headers_for(scope.get("path", ""), api_prefix=self.api_prefix, hsts=self.hsts)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in extra:
                    if name not in headers:
                        headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)
