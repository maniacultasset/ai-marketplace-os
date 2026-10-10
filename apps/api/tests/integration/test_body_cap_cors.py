"""S10 C13 (AC14): the 10 MiB body cap (413 before the read, or once a stream passes it) and CORS
from ALLOWED_ORIGINS, on the real middleware stack. No database is needed: the cap and CORS run
before any dependency (the presence-only fake auth stands in, `tests/support/fake_auth.py`)."""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ORIGIN = "http://localhost:3000"
EVIL = "https://evil.example"
BEARER = {"Authorization": "Bearer t"}


@pytest.fixture
def capped(make_app: Callable[[], FastAPI], monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("REQUEST_MAX_BODY_BYTES", "100")
    monkeypatch.setenv("ALLOWED_ORIGINS", f"{ORIGIN}, http://127.0.0.1:3000")
    return TestClient(make_app(), raise_server_exceptions=False)


def _envelope(response: object, status: int, code: str) -> None:
    assert response.status_code == status, response.text  # type: ignore[attr-defined]
    assert response.json()["error"]["type"] == code  # type: ignore[attr-defined]


def test_body_cap_declared_413(capped: TestClient) -> None:
    response = capped.post("/api/v1/products", content=b"x" * 101, headers=BEARER)
    _envelope(response, 413, "PAYLOAD_TOO_LARGE")


def _chunks(total: int, size: int = 32) -> Iterator[bytes]:
    while total > 0:
        yield b"x" * min(size, total)
        total -= size


def test_body_cap_streamed_413(capped: TestClient) -> None:
    response = capped.post(
        "/api/v1/products",
        content=_chunks(101),
        headers={**BEARER, "content-type": "application/json"},
    )
    _envelope(response, 413, "PAYLOAD_TOO_LARGE")


def test_body_cap_exact_limit_ok(capped: TestClient) -> None:
    response = capped.post(
        "/api/v1/products",
        content=b" " * 98 + b"{}",
        headers={**BEARER, "content-type": "application/json"},
    )
    assert response.status_code == 422  # read in full, then validated
    assert response.json()["error"]["type"] == "VALIDATION_FAILED"


def test_body_cap_no_length_small_ok(capped: TestClient) -> None:
    response = capped.post(
        "/api/v1/products",
        content=_chunks(40),
        headers={**BEARER, "content-type": "application/json"},
    )
    assert response.status_code != 413


def test_body_cap_default_is_10_mib(make_app: Callable[[], FastAPI]) -> None:
    from app.core.config import get_settings

    make_app()
    assert get_settings().request_max_body_bytes == 10 * 1024 * 1024


def test_cors_allowed_origin(capped: TestClient) -> None:
    response = capped.get("/healthz", headers={"Origin": ORIGIN})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert "access-control-allow-credentials" not in response.headers
    exposed = {h.strip() for h in response.headers["access-control-expose-headers"].split(",")}
    assert exposed == {"ETag", "Location", "Retry-After", "X-Request-ID"}
    second = capped.get("/healthz", headers={"Origin": "http://127.0.0.1:3000"})
    assert second.headers["access-control-allow-origin"] == "http://127.0.0.1:3000"


def test_cors_disallowed_origin_no_header(capped: TestClient) -> None:
    response = capped.get("/healthz", headers={"Origin": EVIL})
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_cors_preflight_methods(capped: TestClient) -> None:
    response = capped.options(
        "/api/v1/members/00000000-0000-0000-0000-000000000001",
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "PATCH",
            "Access-Control-Request-Headers": "Authorization, Content-Type, If-Match",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ORIGIN
    methods = {m.strip() for m in response.headers["access-control-allow-methods"].split(",")}
    assert methods == {"GET", "POST", "PATCH", "DELETE"}
    assert "PUT" not in methods
    allowed = response.headers["access-control-allow-headers"].lower()
    for header in ("authorization", "content-type", "idempotency-key", "if-match", "x-request-id"):
        assert header in allowed


def test_cors_delete_preflight_granted(capped: TestClient) -> None:
    """AUTH-1 / GAP3-LIVE-E2E-ACCEPTANCE-1: the S16 DELETE route is reachable from the dashboard
    origin: its preflight (with Authorization) is granted, not refused in the envelope."""
    response = capped.options(
        "/api/v1/brand/documents/00000000-0000-0000-0000-000000000001",
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "DELETE",
            "Access-Control-Request-Headers": "Authorization",
        },
    )
    assert response.status_code == 200, response.text
    assert response.headers["access-control-allow-origin"] == ORIGIN
    methods = {m.strip() for m in response.headers["access-control-allow-methods"].split(",")}
    assert "DELETE" in methods


def test_cors_allows_every_registry_method() -> None:
    """Guard: every method the frozen surface serves is preflight-allowed, so a new verb on a
    route cannot silently be unreachable from a browser on another origin."""
    from app.api.cors import ALLOWED_METHODS
    from app.api.v1.surface import OPS

    assert {op.method for op in OPS.values()} <= set(ALLOWED_METHODS)


def test_cors_exposes_every_documented_response_header(make_app: Callable[[], FastAPI]) -> None:
    """Guard (GAP2-CONTRACT-DOCS-CONFORMANCE-1, GAP3-LIVE-E2E-ACCEPTANCE-2): every response header
    openapi.json documents for clients (ETag for If-Match, Retry-After, Location) is in
    Access-Control-Expose-Headers, or cross-origin JS reads it as null."""
    from app.api.cors import EXPOSED_HEADERS

    spec = make_app().openapi()
    documented = {
        name
        for item in spec["paths"].values()
        for op in item.values()
        for response in op["responses"].values()
        for name in response.get("headers", {})
    }
    assert documented, "the spec documents response headers"
    assert {h.lower() for h in documented} <= {h.lower() for h in EXPOSED_HEADERS}


def test_cors_etag_readable_cross_origin(capped: TestClient) -> None:
    """GAP3-LIVE-E2E-ACCEPTANCE-2: a cross-origin answer names ETag in Expose-Headers."""
    response = capped.get("/healthz", headers={"Origin": ORIGIN})
    exposed = {
        h.strip().lower() for h in response.headers["access-control-expose-headers"].split(",")
    }
    assert {"etag", "retry-after"} <= exposed


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": EVIL, "Access-Control-Request-Method": "POST"},
        {"Origin": ORIGIN, "Access-Control-Request-Method": "PUT"},
        {
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "X-Tenant-Id",
        },
    ],
)
def test_cors_preflight_refused_in_envelope(capped: TestClient, headers: dict[str, str]) -> None:
    response = capped.options("/api/v1/products", headers=headers)
    _envelope(response, 400, "VALIDATION_FAILED")
    assert response.json()["error"]["error_report"]["errors"][0]["source"] == "system"
    if headers["Origin"] == EVIL:
        assert "access-control-allow-origin" not in response.headers


def test_cors_headers_on_error_answers(capped: TestClient) -> None:
    too_big = capped.post(
        "/api/v1/products", content=b"x" * 101, headers={**BEARER, "Origin": ORIGIN}
    )
    assert too_big.status_code == 413
    assert too_big.headers["access-control-allow-origin"] == ORIGIN
    unauthenticated = capped.get("/api/v1/products", headers={"Origin": ORIGIN})
    assert unauthenticated.status_code == 401
    assert unauthenticated.headers["access-control-allow-origin"] == ORIGIN
