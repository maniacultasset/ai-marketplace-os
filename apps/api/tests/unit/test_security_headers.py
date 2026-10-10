"""S26 T8: the security response headers on every answer (app/api/security_headers.py)."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from app.api.security_headers import HSTS, headers_for
from fastapi import FastAPI

NOSNIFF = ("x-content-type-options", "nosniff")
NO_REFERRER = ("referrer-policy", "no-referrer")


def _client(app: FastAPI) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


def _with_boom(app: FastAPI) -> FastAPI:
    async def boom() -> None:
        raise RuntimeError("s26 forced failure /srv/app/secret.py")

    app.add_api_route("/api/v1/__s26_boom", boom, methods=["GET"])
    return app


@pytest.mark.parametrize(
    ("case", "method", "path", "status"),
    [
        ("200", "GET", "/healthz", 200),
        ("404", "GET", "/api/v1/no-such-route", 404),
        ("422", "GET", "/api/v1/products?limit=0", None),
        ("500", "GET", "/api/v1/__s26_boom", 500),
    ],
)
async def test_security_headers_set_on_every_response(
    make_app: Callable[[], FastAPI], case: str, method: str, path: str, status: int | None
) -> None:
    async with _client(_with_boom(make_app())) as client:
        response = await client.request(method, path, headers={"Authorization": "Bearer t"})
    if status is not None:
        assert response.status_code == status, (case, response.text)
    assert response.headers[NOSNIFF[0]] == NOSNIFF[1], case
    assert response.headers[NO_REFERRER[0]] == NO_REFERRER[1], case


async def test_security_headers_no_store_only_api_v1(make_app: Callable[[], FastAPI]) -> None:
    async with _client(make_app()) as client:
        probe = await client.get("/healthz")
        api = await client.get("/api/v1/no-such-route")
    assert "cache-control" not in probe.headers
    assert api.headers["cache-control"] == "no-store"
    assert headers_for("/api/v1", api_prefix="/api/v1", hsts=False)[-1] == (
        "cache-control",
        "no-store",
    )
    assert ("cache-control", "no-store") not in headers_for(
        "/api/v10/x", api_prefix="/api/v1", hsts=False
    )


async def test_security_headers_hsts_off_local_on_staging(
    make_app: Callable[[], FastAPI],
) -> None:
    async with _client(make_app()) as client:
        local = await client.get("/healthz")
    assert HSTS[0] not in local.headers
    assert HSTS in headers_for("/healthz", api_prefix="/api/v1", hsts=True)
    assert HSTS not in headers_for("/healthz", api_prefix="/api/v1", hsts=False)


async def test_security_headers_keep_the_apps_own_value(make_app: Callable[[], FastAPI]) -> None:
    from fastapi.responses import Response

    app = make_app()

    async def own() -> Response:
        return Response(headers={"Cache-Control": "private, max-age=5"})

    app.add_api_route("/api/v1/__s26_own", own, methods=["GET"])
    async with _client(app) as client:
        response = await client.get("/api/v1/__s26_own")
    assert response.headers["cache-control"] == "private, max-age=5"
