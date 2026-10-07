"""`StaffGate`: the ASGI gate in front of every `/internal/admin` route (S14b T17; D9; ENV.md:26-27).

- `APP_ENV=local`: the staff member is `ADMIN_LOCAL_EMAIL`. Anywhere else every request is 403
  until the staff identity source (IAP/IAM, S4) exists; a tenant JWT never opens the admin.
- Only GET and HEAD: any other method is 405, so no sqladmin form or action can write.
- Every response carries `Cache-Control: no-store`, `X-Frame-Options: DENY`,
  `Referrer-Policy: no-referrer` and `X-Content-Type-Options: nosniff`.
- One `admin.view` log line per request with the staff member and the path (S11 redaction
  applies to the `staff_email` field like any other e-mail key).
"""

from __future__ import annotations

from typing import Final

from starlette.datastructures import MutableHeaders
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.logging import get_logger

log = get_logger(__name__)
READ_METHODS: Final = frozenset({"GET", "HEAD"})
SECURITY_HEADERS: Final = (
    ("cache-control", "no-store"),
    ("x-frame-options", "DENY"),
    ("referrer-policy", "no-referrer"),
    ("x-content-type-options", "nosniff"),
)


class StaffGate:
    def __init__(self, app: ASGIApp, *, app_env: str, local_email: str) -> None:
        self.app = app
        self.staff = local_email if app_env == "local" else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS:
                    headers[name] = value
            await send(message)

        path = scope.get("path", "")  # the full path: Starlette keeps the mount prefix in it
        method = scope.get("method", "GET")
        log.info("admin.view", staff_email=self.staff, path=path, method=method)
        if self.staff is None:
            await PlainTextResponse("Forbidden", status_code=403)(scope, receive, send_with_headers)
            return
        if method not in READ_METHODS:
            response = PlainTextResponse(
                "Method Not Allowed", status_code=405, headers={"Allow": "GET, HEAD"}
            )
            await response(scope, receive, send_with_headers)
            return
        await self.app(scope, receive, send_with_headers)
