"""The one HTTP client the OAuth clients use: short timeouts, no redirects, no environment
(proxies, netrc) and no event hooks, so nothing logs a request, a form or a reply.
`make_transport()` returns None in the app; tests swap it for an `httpx.ASGITransport` over the
fake marketplace (no network)."""

from __future__ import annotations

from typing import Final

import httpx

TIMEOUT: Final = httpx.Timeout(10.0, connect=5.0)


def make_transport() -> httpx.AsyncBaseTransport | None:
    return None


def oauth_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=make_transport(),
        timeout=TIMEOUT,
        follow_redirects=False,
        trust_env=False,
    )
