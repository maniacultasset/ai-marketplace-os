"""S26 T8 (AC 2, OWASP API8, C2): security misconfiguration.

CORS grants only the configured origins (never `*`, never credentials, a disallowed preflight
gets no grant); an unhandled exception is the envelope with a correlation id and nothing of the
exception; production serves no `/docs`, `/redoc` or `/openapi.json`; no `Server` version leaks;
a 405 is the envelope; the security headers are on every answer.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

ALLOWED = "http://localhost:3000"
LOOKALIKES = (
    "http://evil.example",
    "null",
    "http://localhost:3000.evil.example",
    "http://evil-localhost:3000",
    "https://localhost:3000",
    "http://localhost:3001",
)


@pytest.fixture
async def client(auth_db: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    from app.core.config import get_settings
    from app.main import create_app

    monkeypatch.setenv("ALLOWED_ORIGINS", ALLOWED)
    get_settings.cache_clear()
    app = create_app()

    async def boom() -> None:
        raise RuntimeError("s26-boom /srv/app/secret_module.py password=hunter2")

    app.add_api_route("/api/v1/__s26_boom", boom, methods=["GET"])
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
    get_settings.cache_clear()


@pytest.mark.parametrize("origin", LOOKALIKES)
async def test_api8_disallowed_origin_no_acao(client: httpx.AsyncClient, origin: str) -> None:
    response = await client.get("/healthz", headers={"Origin": origin})
    assert "access-control-allow-origin" not in response.headers, origin
    assert "access-control-allow-credentials" not in response.headers


async def test_api8_allowed_origin_acao(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz", headers={"Origin": ALLOWED})
    assert response.headers["access-control-allow-origin"] == ALLOWED
    assert "access-control-allow-credentials" not in response.headers


@pytest.mark.parametrize("origin", LOOKALIKES)
async def test_api8_preflight_disallowed_no_acao(client: httpx.AsyncClient, origin: str) -> None:
    response = await client.options(
        "/api/v1/products",
        headers={"Origin": origin, "Access-Control-Request-Method": "POST"},
    )
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers
    assert response.json()["error"]["type"] == "VALIDATION_FAILED"


async def test_api8_no_wildcard_origin(client: httpx.AsyncClient) -> None:
    from app.api.cors import ApiCORSMiddleware
    from app.main import create_app

    for middleware in create_app().user_middleware:
        if middleware.cls is ApiCORSMiddleware:  # type: ignore[comparison-overlap]
            assert "*" not in str(middleware.kwargs["allowed_origins"])
    preflight = await client.options(
        "/api/v1/products",
        headers={"Origin": ALLOWED, "Access-Control-Request-Method": "POST"},
    )
    assert preflight.headers["access-control-allow-origin"] == ALLOWED
    assert preflight.headers.get("access-control-allow-origin") != "*"


async def test_api8_forced_500_envelope(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/__s26_boom")
    assert response.status_code == 500
    body = response.json()
    assert body["error"]["type"] == "INTERNAL_ERROR"
    assert body["error"]["correlation_id"]
    for leak in ("Traceback", "secret_module", "/srv/app", "hunter2", "RuntimeError", "s26-boom"):
        assert leak not in response.text, leak
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
async def test_api8_docs_and_openapi_404_in_production(
    prod_client: httpx.AsyncClient, path: str
) -> None:
    response = await prod_client.get(path)
    assert response.status_code == 404, response.text
    assert response.json()["error"]["type"] == "NOT_FOUND"
    assert "strict-transport-security" in response.headers


async def test_api8_no_server_version_header(client: httpx.AsyncClient) -> None:
    for path in ("/healthz", "/api/v1/products", "/api/v1/__s26_boom", "/no-such"):
        response = await client.get(path)
        assert "server" not in response.headers, path
        assert "x-powered-by" not in response.headers, path


async def test_api8_405_is_envelope(client: httpx.AsyncClient) -> None:
    response = await client.delete("/api/v1/products")
    assert response.status_code == 405
    assert response.json()["error"]["type"] == "METHOD_NOT_ALLOWED"
    assert response.json()["error"]["correlation_id"]
    assert "GET" in response.headers["allow"]
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
