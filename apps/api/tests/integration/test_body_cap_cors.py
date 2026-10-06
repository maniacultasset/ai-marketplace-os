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
    assert response.headers["access-control-expose-headers"] == "X-Request-ID"
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
    assert {"GET", "POST", "PATCH"} <= methods
    assert "DELETE" not in methods
    allowed = response.headers["access-control-allow-headers"].lower()
    for header in ("authorization", "content-type", "idempotency-key", "if-match", "x-request-id"):
        assert header in allowed


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": EVIL, "Access-Control-Request-Method": "POST"},
        {"Origin": ORIGIN, "Access-Control-Request-Method": "DELETE"},
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
