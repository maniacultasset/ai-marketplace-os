"""S26 tester (AC 2 API8): the security headers on the answers the T8 suite does not name.

A 413 from the body cap, a CORS preflight (allowed and refused), a 405 and a HEAD (the
production `/docs` 404 with HSTS is in tests/security/test_owasp_api8_config.py).
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from fastapi import FastAPI

NOSNIFF = ("x-content-type-options", "nosniff")
NO_REFERRER = ("referrer-policy", "no-referrer")
ALLOWED_ORIGIN = "http://localhost:3000"


def _client(app: FastAPI) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


def _assert_headers(response: httpx.Response, case: str, *, api: bool) -> None:
    assert response.headers.get(NOSNIFF[0]) == NOSNIFF[1], (case, dict(response.headers))
    assert response.headers.get(NO_REFERRER[0]) == NO_REFERRER[1], (case, dict(response.headers))
    if api:
        assert response.headers.get("cache-control") == "no-store", (case, dict(response.headers))


async def test_headers_on_413_body_cap(make_app: Callable[[], FastAPI]) -> None:
    async with _client(make_app()) as client:
        response = await client.post(
            "/api/v1/products",
            content=b"x" * (10 * 1024 * 1024 + 1),
            headers={"Authorization": "Bearer t", "Content-Type": "application/json"},
        )
    assert response.status_code == 413, response.text
    _assert_headers(response, "413", api=True)


@pytest.mark.parametrize("origin", [ALLOWED_ORIGIN, "https://evil.example"])
async def test_headers_on_cors_preflight(make_app: Callable[[], FastAPI], origin: str) -> None:
    async with _client(make_app()) as client:
        response = await client.options(
            "/api/v1/products",
            headers={"Origin": origin, "Access-Control-Request-Method": "POST"},
        )
    _assert_headers(response, f"preflight {origin}", api=True)
    if origin != ALLOWED_ORIGIN:
        assert "access-control-allow-origin" not in response.headers


async def test_headers_on_405_and_head(make_app: Callable[[], FastAPI]) -> None:
    async with _client(make_app()) as client:
        not_allowed = await client.request(
            "PUT", "/api/v1/products", headers={"Authorization": "Bearer t"}
        )
        head = await client.head("/healthz")
    assert not_allowed.status_code == 405, not_allowed.text
    _assert_headers(not_allowed, "405", api=True)
    _assert_headers(head, "HEAD /healthz", api=False)
