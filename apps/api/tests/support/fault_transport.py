"""`FaultTransport`: an httpx transport that delegates a write, then raises (S20 T9).

`httpx.ASGITransport` ignores timeouts, so a test that needs "the marketplace applied the write
but the reply never came" wraps the fake's transport: the request reaches the fake (which
applies it), then the configured exception is raised in place of the reply.
"""

from __future__ import annotations

import httpx


class FaultTransport(httpx.AsyncBaseTransport):
    def __init__(
        self,
        inner: httpx.AsyncBaseTransport,
        *,
        after_write: Exception | None = None,
        before_send: Exception | None = None,
        times: int = 1,
    ) -> None:
        self.inner = inner
        self.after_write = after_write
        self.before_send = before_send
        self.times = times
        self.in_flight = 0
        self.max_in_flight = 0
        self.sent = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self.before_send is not None and self.times > 0:
            self.times -= 1
            raise self.before_send
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            response = await self.inner.handle_async_request(request)
            self.sent += 1
        finally:
            self.in_flight -= 1
        if self.after_write is not None and self.times > 0 and request.method in ("PUT", "PATCH"):
            self.times -= 1
            await response.aclose()
            raise self.after_write
        return response
