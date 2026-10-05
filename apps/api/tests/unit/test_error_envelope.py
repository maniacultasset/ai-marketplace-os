"""S3 T3 (AC2-AC4): one error envelope for every non-2xx, built in app/api/errors.py."""

from __future__ import annotations

import re
import uuid
from typing import Annotated, Any

import pytest
from app.api.errors import (
    CORRELATION_ID_PATTERN,
    STATUS_CODES,
    ApiError,
    ErrorEnvelope,
    NotImplementedYet,
    code_for_status,
    correlation_id,
    install_error_handlers,
)
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st
from mkos_schemas.base import RequestId, StrictModel
from mkos_schemas.codes import ErrorCode
from pydantic import Field, SecretStr, TypeAdapter
from starlette.requests import Request

CANARY = "CANARYc4n4ry0123456789"


class Body(StrictModel):
    name: Annotated[str, Field(max_length=8)]
    count: int
    password: SecretStr | None = None


def _app() -> FastAPI:
    app = FastAPI(redirect_slashes=False)
    install_error_handlers(app)

    @app.get("/api-error")
    async def api_error() -> None:
        raise ApiError(409, ErrorCode.SKU_CONFLICT, "SKU already exists.")

    @app.get("/api-error-401")
    async def api_error_401() -> None:
        raise ApiError(401, ErrorCode.UNAUTHENTICATED, "Authentication required.")

    @app.get("/http/{status}")
    async def http_error(status: int) -> None:
        raise HTTPException(status_code=status, detail=f"detail {CANARY}")

    @app.get("/stub")
    async def stub() -> None:
        raise NotImplementedYet("products_list")

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError(f"secret internals {CANARY}")

    @app.post("/body")
    async def body(payload: Body) -> None:
        return None

    @app.get("/thing/{thing_id}")
    async def thing(thing_id: uuid.UUID, limit: Annotated[int, Query(ge=1)] = 1) -> None:
        return None

    @app.get("/header")
    async def header(key: Annotated[str, Header(alias="Idempotency-Key")]) -> None:
        return None

    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(_app(), raise_server_exceptions=False)


def _assert_envelope(response: Any, status: int, code: ErrorCode) -> dict[str, Any]:
    assert response.status_code == status
    body: dict[str, Any] = response.json()
    ErrorEnvelope.model_validate(body)
    error = body["error"]
    assert error["type"] == code.value
    assert error["status"] == status
    assert error["correlation_id"]
    assert response.headers["x-request-id"] == error["correlation_id"]
    report = error["error_report"]
    assert report["status"] == "failed"
    assert report["error_code"] == code.value
    assert report["request_id"] == error["correlation_id"]
    assert len(report["errors"]) == 1
    item = report["errors"][0]
    assert item["field"] == "request"
    assert item["source"] == "system"
    assert item["error_code"] == code.value
    return body


def test_api_error_handler(client: TestClient) -> None:
    body = _assert_envelope(client.get("/api-error"), 409, ErrorCode.SKU_CONFLICT)
    assert body["error"]["message"] == "SKU already exists."
    assert body["error"]["details"] is None


def test_401_carries_www_authenticate(client: TestClient) -> None:
    response = client.get("/api-error-401")
    _assert_envelope(response, 401, ErrorCode.UNAUTHENTICATED)
    assert response.headers["www-authenticate"] == "Bearer"


def test_http_exception_handler_never_echoes_detail(client: TestClient) -> None:
    for status in (400, 403, 404, 409, 413, 429):
        response = client.get(f"/http/{status}")
        _assert_envelope(response, status, STATUS_CODES[status])
        assert CANARY not in response.text


def test_not_implemented_handler(client: TestClient) -> None:
    body = _assert_envelope(client.get("/stub"), 501, ErrorCode.NOT_IMPLEMENTED)
    assert "products_list" in body["error"]["message"]


def test_unknown_path_404(client: TestClient) -> None:
    _assert_envelope(client.get("/nope"), 404, ErrorCode.NOT_FOUND)


def test_wrong_method_405_keeps_allow(client: TestClient) -> None:
    response = client.delete("/stub")
    _assert_envelope(response, 405, ErrorCode.METHOD_NOT_ALLOWED)
    assert response.headers["allow"] == "GET"


def test_500_no_traceback(client: TestClient) -> None:
    response = client.get("/boom")
    _assert_envelope(response, 500, ErrorCode.INTERNAL_ERROR)
    assert "Traceback" not in response.text
    assert CANARY not in response.text
    assert "RuntimeError" not in response.text


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, ErrorCode.VALIDATION_FAILED),
        (401, ErrorCode.UNAUTHENTICATED),
        (403, ErrorCode.PERMISSION_DENIED),
        (404, ErrorCode.NOT_FOUND),
        (405, ErrorCode.METHOD_NOT_ALLOWED),
        (409, ErrorCode.IDEMPOTENCY_KEY_REUSED),
        (413, ErrorCode.PAYLOAD_TOO_LARGE),
        (422, ErrorCode.VALIDATION_FAILED),
        (429, ErrorCode.RATE_LIMITED),
        (501, ErrorCode.NOT_IMPLEMENTED),
        (500, ErrorCode.INTERNAL_ERROR),
        (502, ErrorCode.INTERNAL_ERROR),
        (503, ErrorCode.INTERNAL_ERROR),
        (418, ErrorCode.VALIDATION_FAILED),
    ],
)
def test_status_map_table(status: int, code: ErrorCode) -> None:
    assert code_for_status(status) is code


def test_429_is_retryable(client: TestClient) -> None:
    # L6: retryable follows ERROR_CODE_META (RATE_LIMITED is auto_retry); the rest are not.
    rate = client.get("/http/429").json()["error"]["error_report"]
    assert rate["retryable"] is True
    assert rate["errors"][0]["retryable"] is True
    other = client.get("/http/404").json()["error"]["error_report"]
    assert other["retryable"] is False


def test_422_details_are_loc_msg_only(client: TestClient) -> None:
    response = client.post("/body", json={"name": "x" * 20, "count": "nope", "extra": 1})
    body = _assert_envelope(response, 422, ErrorCode.VALIDATION_FAILED)
    details = body["error"]["details"]
    assert details
    for detail in details:
        assert set(detail) == {"loc", "msg"}
    locs = {tuple(d["loc"]) for d in details}
    assert ("body", "name") in locs
    assert ("body", "count") in locs
    assert ("body", "extra") in locs


def test_never_echoes_value(client: TestClient) -> None:
    too_long = client.post("/body", json={"name": CANARY, "count": 1})
    wrong_type = client.post("/body", json={"name": "a", "count": CANARY})
    bad_uuid = client.get(f"/thing/{CANARY}")
    bad_query = client.get(f"/thing/{uuid.uuid4()}?limit={CANARY}")
    secret = client.post("/body", json={"name": "a", "count": "x", "password": CANARY})
    for response in (too_long, wrong_type, bad_uuid, bad_query, secret):
        _assert_envelope(response, 422, ErrorCode.VALIDATION_FAILED)
        assert CANARY not in response.text
        assert "c4n4ry" not in response.text


def test_unsafe_key_names_are_masked(client: TestClient) -> None:
    # Key names are structure, not values: an identifier-like extra key is named so the client
    # can fix it; anything else (spaces, Unicode, over 64 chars) becomes `<key>`.
    named = client.post("/body", json={"name": "a", "count": 1, "reviewer_id": 1})
    assert named.json()["error"]["details"][0]["loc"] == ["body", "reviewer_id"]
    for key in (f"{CANARY} x", f"é{CANARY}", "k" * 65, "<script>"):
        response = client.post("/body", json={"name": "a", "count": 1, key: 1})
        _assert_envelope(response, 422, ErrorCode.VALIDATION_FAILED)
        assert response.json()["error"]["details"][0]["loc"] == ["body", "<key>"]
        assert key not in response.text


def test_uuid_error_hides_the_bad_character(client: TestClient) -> None:
    # pydantic's uuid_parsing message quotes the first bad character; the envelope must not.
    response = client.get("/thing/zzzz")
    msg = response.json()["error"]["details"][0]["msg"]
    assert "`z`" not in msg
    assert msg == "Input should be a valid UUID."


def test_missing_header_422(client: TestClient) -> None:
    body = _assert_envelope(client.get("/header"), 422, ErrorCode.VALIDATION_FAILED)
    assert body["error"]["details"][0]["loc"] == ["header", "Idempotency-Key"]


def test_malformed_json_422(client: TestClient) -> None:
    response = client.post(
        "/body", content=f'{{"name": "{CANARY}"', headers={"content-type": "application/json"}
    )
    body = _assert_envelope(response, 422, ErrorCode.VALIDATION_FAILED)
    assert CANARY not in response.text
    assert body["error"]["details"][0]["msg"] == "Malformed JSON body."


def test_envelope_validates(client: TestClient) -> None:
    for path in ("/api-error", "/stub", "/boom", "/nope", "/http/404", f"/thing/{CANARY}"):
        ErrorEnvelope.model_validate(client.get(path).json())


def _request(headers: list[tuple[bytes, bytes]]) -> Request:
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers})


def test_correlation_id_echo_and_replace() -> None:
    kept = "req-01.ab:CD_9"
    assert correlation_id(_request([(b"x-request-id", kept.encode())])) == kept
    assert correlation_id(_request([(b"x-request-id", b"a" * 128)])) == "a" * 128
    for bad in (b"", b"a" * 129, b"a\r\nb", b"a b", "é".encode(), b"\x00"):
        got = correlation_id(_request([(b"x-request-id", bad)]))
        assert got != bad.decode("latin-1")
        uuid.UUID(got)
    two = _request([(b"x-request-id", b"one"), (b"x-request-id", b"two")])
    uuid.UUID(correlation_id(two))
    uuid.UUID(correlation_id(_request([])))


def test_correlation_id_cached_per_request() -> None:
    request = _request([])
    assert correlation_id(request) == correlation_id(request)


_REQUEST_ID = TypeAdapter(RequestId)


@settings(max_examples=200, deadline=None)
@given(st.text(max_size=200))
def test_correlation_id_always_valid(value: str) -> None:
    got = correlation_id(_request([(b"x-request-id", value.encode("utf-8"))]))
    assert re.fullmatch(CORRELATION_ID_PATTERN, got)
    _REQUEST_ID.validate_python(got)
