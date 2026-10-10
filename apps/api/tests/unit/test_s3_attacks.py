"""S3 tester attacks on the frozen `/api/v1` surface (AC2-AC4, AC7, AC12, AC16).

Independent of the implementer's tests: edge inputs the surface must answer with the one
envelope, never a 500, a redirect or an echo of the input.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from app.api.errors import ErrorEnvelope
from fastapi import FastAPI
from fastapi.testclient import TestClient

BEARER = {"Authorization": "Bearer t"}
# a GET whose error envelope the request id rides on, with no database behind the app: no
# /api/v1 GET is still a 501 stub since S23 implemented analytics, so an unrouted path's 404
ERROR_GET = "/api/v1/analytics/no-such-subroute"
IDEM = {"Idempotency-Key": "tester-key-1"}
ID = "0b1c2d3e-0000-4000-8000-00000000abcd"
VERSION_ID = "0b1c2d3e-0000-4000-8000-00000000abce"
SECRET = "Tester-Secret-Value-9f8e7d"


@pytest.fixture(scope="module")
def client(v1_app: FastAPI) -> TestClient:
    return TestClient(v1_app, raise_server_exceptions=False)


def _envelope(response: Any, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body: dict[str, Any] = response.json()
    ErrorEnvelope.model_validate(body)
    assert body["error"]["type"] == code
    assert body["error"]["error_report"]["error_code"] == code
    assert response.headers["X-Request-ID"] == body["error"]["correlation_id"]
    assert SECRET not in response.text
    return body


# ---- correlation id ----


@pytest.mark.parametrize(
    "value", ["a" * 129, "has space", "semi;colon", 'quote"x', "\u00e9t\u00e9".encode()]
)
def test_invalid_request_id_gets_fresh_uuid(client: TestClient, value: str | bytes) -> None:
    # bytes header values on purpose; the TestClient's stub types only `str` (httpx2 since S16)
    headers: Any = {**BEARER, "X-Request-ID": value}
    r = client.get(ERROR_GET, headers=headers)
    body = _envelope(r, 404, "NOT_FOUND")
    uuid.UUID(body["error"]["correlation_id"])


def test_valid_request_id_is_kept(client: TestClient) -> None:
    r = client.get(ERROR_GET, headers={**BEARER, "X-Request-ID": "fe:trace.01-x"})
    assert _envelope(r, 404, "NOT_FOUND")["error"]["correlation_id"] == "fe:trace.01-x"


# ---- routing edges (AC4) ----


@pytest.mark.parametrize("method", ["OPTIONS", "DELETE", "PUT"])
def test_unserved_methods_are_405_with_full_allow(client: TestClient, method: str) -> None:
    r = client.request(method, "/api/v1/products", headers=BEARER)
    _envelope(r, 405, "METHOD_NOT_ALLOWED")
    assert r.headers["Allow"] == "GET, POST"


def test_head_is_405_not_a_silent_get(client: TestClient) -> None:
    r = client.head("/api/v1/products", headers=BEARER)
    assert r.status_code == 405
    assert r.headers["Allow"] == "GET, POST"


@pytest.mark.parametrize(
    "path", ["/api/v1/products/", "/api/v1//products", "/API/V1/products", "/v1/products"]
)
def test_lookalike_paths_are_404_never_redirect(client: TestClient, path: str) -> None:
    r = client.get(path, headers=BEARER, follow_redirects=False)
    _envelope(r, 404, "NOT_FOUND")


def test_get_on_literal_post_path_never_answers_200(client: TestClient) -> None:
    # /listings/validate is POST only; GET would fall to /listings/{listing_id} and fail the
    # UUID, so the shadowed static path answers 405 with its own Allow (S26 fix round 1).
    r = client.get("/api/v1/listings/validate", headers=BEARER)
    _envelope(r, 405, "METHOD_NOT_ALLOWED")
    assert r.headers["Allow"] == "POST"


# ---- presence-only bearer (AC3) ----


@pytest.mark.parametrize(
    "header",
    [
        {"Authorization": "Basic dTpw"},
        {"Authorization": "Bearer    "},
        {"Authorization": "Bearer"},
        {},
    ],
)
def test_non_bearer_or_blank_is_401(client: TestClient, header: dict[str, str]) -> None:
    r = client.get("/api/v1/auth/me", headers=header)
    _envelope(r, 401, "UNAUTHENTICATED")
    assert r.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "params", [{"limit": "0"}, {"limit": "abc"}, {"cursor": "!!"}, {"cursor": "a" * 600}]
)
def test_auth_is_checked_before_query_validation(
    client: TestClient, params: dict[str, str]
) -> None:
    r = client.get("/api/v1/products", params=params)
    _envelope(r, 401, "UNAUTHENTICATED")


# ---- pagination (AC7) ----


@pytest.mark.parametrize(
    "cursor",
    [
        "eyJ0IjoxfQ",  # {"t":1}
        "eyJ0IjoiMjAyNi0xMC0wNVQwMDowMDowMCIsImlkIjoiMGIxYzJkM2UtMDAwMC00MDAwLTgwMDAtMDAwMDAwMDBhYmNkIn0",  # naive t
        "bnVsbA",  # null
        "W10",  # []
    ],
)
def test_foreign_cursors_are_422(client: TestClient, cursor: str) -> None:
    r = client.get("/api/v1/products", headers=BEARER, params={"cursor": cursor})
    body = _envelope(r, 422, "VALIDATION_FAILED")
    assert body["error"]["details"] == [{"loc": ["query", "cursor"], "msg": "Invalid cursor."}]


# ---- headers (D9) ----


@pytest.mark.parametrize("key", ["", "has space", "x" * 256, "tab\tkey", "caf\u00e9".encode()])
def test_bad_idempotency_key_is_422(client: TestClient, key: str | bytes) -> None:
    headers: Any = {**BEARER, "Idempotency-Key": key}
    r = client.post(
        "/api/v1/listings/generate",
        headers=headers,
        json={"product_id": ID, "marketplace": "amazon"},
    )
    _envelope(r, 422, "VALIDATION_FAILED")


@pytest.mark.parametrize("etag", [f'W/"{VERSION_ID}"', VERSION_ID, "*", f'"{VERSION_ID}", "{ID}"'])
def test_bad_if_match_is_422(client: TestClient, etag: str) -> None:
    r = client.patch(
        f"/api/v1/listings/{ID}/content",
        headers={**BEARER, "If-Match": etag},
        json={"content": {"title": "T", "bullets": ["b"], "description": "d"}},
    )
    _envelope(r, 422, "VALIDATION_FAILED")


# ---- request bodies (AC3, AC8) ----


def _approve_item(listing_id: str = ID, **extra: Any) -> dict[str, Any]:
    return {
        "listing_id": listing_id,
        "listing_version_id": VERSION_ID,
        "decision": "approve",
        **extra,
    }


@pytest.mark.parametrize(
    "items",
    [
        [],
        [_approve_item()] * 2,
        [_approve_item(f"0b1c2d3e-0000-4000-8000-{i:012d}") for i in range(201)],
        [_approve_item(reviewer_id=ID)],
        [_approve_item(tenant_id=ID)],
        [_approve_item(decision="reject")],
    ],
    ids=["empty", "duplicate", "201", "reviewer_id", "tenant_id", "reject_without_reason"],
)
def test_bad_approve_batches_are_422(client: TestClient, items: list[dict[str, Any]]) -> None:
    r = client.post("/api/v1/listings/approve", headers={**BEARER, **IDEM}, json={"items": items})
    _envelope(r, 422, "VALIDATION_FAILED")


def test_two_hundred_items_is_accepted() -> None:
    """The bound is 1..200 (S19 runs the route; its integration tests cover the behaviour)."""
    from app.api.v1.schemas.batches import ApproveRequest

    items = [_approve_item(f"0b1c2d3e-0000-4000-8000-{i:012d}") for i in range(200)]
    assert len(ApproveRequest.model_validate({"items": items}).items) == 200


@pytest.mark.parametrize(
    "key",
    [
        "../../etc/passwd",
        "/etc/passwd",
        "a/%2e%2e/b",
        "a\\..\\b",
        "a//b",
        "a/",
        "https://x.io/a.jpg",
        "a/../b",
        "\u0000a",
    ],
)
def test_storage_key_shapes_are_422(client: TestClient, key: str) -> None:
    r = client.post(
        "/api/v1/images/upload",
        headers={**BEARER, **IDEM},
        data={"product_id": ID, "slot": "main", "storage_key": key},
    )
    _envelope(r, 422, "VALIDATION_FAILED")


def test_nan_and_infinity_are_422(client: TestClient) -> None:
    for token in (b"NaN", b"Infinity", b"-Infinity"):
        r = client.post(
            "/api/v1/products",
            headers={**BEARER, "content-type": "application/json"},
            content=b'{"sku":"A1","title":"K","attributes":{"w":' + token + b"}}",
        )
        _envelope(r, 422, "VALIDATION_FAILED")


def test_deeply_nested_json_is_an_envelope_not_a_500(client: TestClient) -> None:
    r = client.post(
        "/api/v1/products",
        headers={**BEARER, "content-type": "application/json"},
        content=b"[" * 100_000 + b"]" * 100_000,
    )
    assert r.status_code in {400, 422}, r.text
    ErrorEnvelope.model_validate(r.json())


@pytest.mark.parametrize(
    "body",
    [
        {"email": "owner@fabrica.example", "password": SECRET, SECRET: SECRET},
        {"email": SECRET, "password": SECRET},
        {"email": "owner@fabrica.example", "password": [SECRET]},
        {"email": "owner@fabrica.example", "password": SECRET * 100},
    ],
)
def test_login_422_never_echoes_secret_values(client: TestClient, body: dict[str, Any]) -> None:
    r = client.post("/api/v1/auth/login", json=body)
    assert r.status_code == 422
    ErrorEnvelope.model_validate(r.json())
    # Key names may be kept in `loc` (DEVIATIONS D-3); the value must never appear in a message.
    for detail in r.json()["error"]["details"]:
        assert SECRET not in detail["msg"]


# ---- body cap (AC16) ----


def _chunks(total_mib: int) -> Iterator[bytes]:
    for _ in range(total_mib):
        yield b"x" * (1024 * 1024)


@pytest.mark.parametrize(
    ("path", "headers"),
    [
        ("/api/v1/products", {**BEARER, "content-type": "application/json"}),
        ("/api/v1/auth/login", {"content-type": "application/json"}),
        ("/api/v1/images/upload", {"content-type": "multipart/form-data; boundary=zz"}),
        ("/api/v1/brand/documents", {"content-type": "multipart/form-data; boundary=zz"}),
    ],
)
def test_streamed_body_over_cap_is_413(
    client: TestClient, path: str, headers: dict[str, str]
) -> None:
    r = client.post(path, headers=headers, content=_chunks(11))
    _envelope(r, 413, "PAYLOAD_TOO_LARGE")


@pytest.mark.parametrize("value", ["+5", "5, 5", "0x10", "1e3", " "])
def test_malformed_content_length_is_400(client: TestClient, value: str) -> None:
    r = client.post("/api/v1/products", headers={**BEARER, "content-length": value}, content=b"")
    _envelope(r, 400, "VALIDATION_FAILED")


# ---- mock (AC12) ----


@pytest.fixture(scope="module")
def mock_client() -> TestClient:
    from app_mock.server import create_mock_app

    spec = Path(__file__).resolve().parents[4] / "packages/schemas/jsonschema/openapi.json"
    return TestClient(create_mock_app(spec))


def test_mock_refuses_an_undeclared_prefer_code(mock_client: TestClient) -> None:
    r = mock_client.get("/api/v1/products", headers={"Prefer": "code=404"})
    body = _envelope_mock(r, 422, "VALIDATION_FAILED")
    assert "code=404" in body["error"]["message"]


def test_mock_honours_prefer_among_other_preferences(mock_client: TestClient) -> None:
    r = mock_client.get(f"/api/v1/products/{ID}", headers={"Prefer": "respond-async, code=404"})
    _envelope_mock(r, 404, "NOT_FOUND")


def test_mock_callback_redirect_carries_only_pending(mock_client: TestClient) -> None:
    r = mock_client.get("/api/v1/marketplaces/amazon/callback", follow_redirects=False)
    assert r.status_code == 303
    location = r.headers["Location"]
    assert "pending=" in location
    assert "code=" not in location and "token" not in location


def _envelope_mock(response: Any, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body: dict[str, Any] = response.json()
    ErrorEnvelope.model_validate(body)
    assert body["error"]["type"] == code
    return body
