"""S12 T1 (AC2, AC4): domain errors raised below the HTTP layer render as the one envelope.

`ConflictError` is 409 with its code (an unmapped constraint, `INTERNAL_ERROR`, is 500),
`NotFoundError` is 404 `NOT_FOUND` with `details` null, and `InputError` is 422 with its code and
`details = [{loc, msg}]`. No body echoes the input value, and none carries a `field` key."""

from __future__ import annotations

import json
from typing import Any

import pytest
from app.api.errors import ErrorEnvelope, install_error_handlers
from app.core.errors import ConflictError, InputError, NotFoundError
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mkos_schemas.codes import ErrorCode

CANARY = "CANARYs12t1v4lue0123"


def _app() -> FastAPI:
    from app.core.asgi import MkosApp

    app = MkosApp()
    install_error_handlers(app)

    @app.get("/conflict/{code}")
    async def conflict(code: str) -> None:
        raise ConflictError(ErrorCode(code), f"some_constraint_{CANARY}")

    @app.get("/not-found")
    async def not_found() -> None:
        raise NotFoundError("product_id")

    @app.get("/input/{code}")
    async def input_error(code: str, field: str) -> None:
        raise InputError(ErrorCode(code), field)

    @app.get("/canary")
    async def canary() -> None:
        error = InputError(ErrorCode.VALIDATION_FAILED, "title")
        error.args = (CANARY,)
        raise error

    return app


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(_app(), raise_server_exceptions=False)


def _envelope(response: Any) -> dict[str, Any]:
    ErrorEnvelope.model_validate(response.json())
    body: dict[str, Any] = response.json()["error"]
    return body


def _keys(node: Any) -> set[str]:
    if isinstance(node, dict):
        return set(node) | {k for v in node.values() for k in _keys(v)}
    if isinstance(node, list):
        return {k for v in node for k in _keys(v)}
    return set()


def test_conflict_maps_409_with_code(client: TestClient) -> None:
    response = client.get("/conflict/SKU_CONFLICT")
    assert response.status_code == 409
    body = _envelope(response)
    assert body["type"] == "SKU_CONFLICT"
    assert body["status"] == 409
    assert body["details"] is None
    assert body["error_report"]["error_code"] == "SKU_CONFLICT"


def test_conflict_internal_error_is_500(client: TestClient) -> None:
    response = client.get("/conflict/INTERNAL_ERROR")
    assert response.status_code == 500
    body = _envelope(response)
    assert body["type"] == "INTERNAL_ERROR"
    assert CANARY not in response.text


def test_not_found_404_details_null(client: TestClient) -> None:
    response = client.get("/not-found")
    assert response.status_code == 404
    body = _envelope(response)
    assert body["type"] == "NOT_FOUND"
    assert body["details"] is None
    assert body["message"] == "product_id not found."
    assert "field" not in _keys(body["details"])


@pytest.mark.parametrize(
    ("field", "loc"),
    [
        ("attributes.color", ["body", "attributes", "color"]),
        ("images[0]", ["body", "images", 0]),
        ("sku", ["body", "sku"]),
    ],
)
def test_input_error_422_details_loc_msg(client: TestClient, field: str, loc: list[Any]) -> None:
    response = client.get("/input/VALIDATION_FAILED", params={"field": field})
    assert response.status_code == 422
    body = _envelope(response)
    assert body["details"] == [{"loc": loc, "msg": "Invalid value."}]
    assert "field" not in _keys(body["details"])


def test_input_error_422_unknown_fact_key(client: TestClient) -> None:
    response = client.get("/input/UNKNOWN_FACT_KEY", params={"field": "attributes.shade"})
    assert response.status_code == 422
    body = _envelope(response)
    assert body["type"] == "UNKNOWN_FACT_KEY"
    assert body["error_report"]["error_code"] == "UNKNOWN_FACT_KEY"
    assert body["details"] == [{"loc": ["body", "attributes", "shade"], "msg": "Unknown key."}]


def test_input_error_422_validation_failed(client: TestClient) -> None:
    response = client.get("/input/VALIDATION_FAILED", params={"field": "title"})
    assert response.status_code == 422
    assert _envelope(response)["type"] == "VALIDATION_FAILED"


def test_input_error_unsafe_key_is_masked(client: TestClient) -> None:
    response = client.get("/input/VALIDATION_FAILED", params={"field": f"attributes.{CANARY}!"})
    assert response.status_code == 422
    assert CANARY not in response.text
    assert _envelope(response)["details"][0]["loc"] == ["body", "attributes", "<key>"]


def test_error_details_never_echo_value(client: TestClient) -> None:
    response = client.get("/canary")
    assert response.status_code == 422
    assert CANARY not in response.text
    conflict = client.get("/conflict/SKU_CONFLICT")
    assert CANARY not in conflict.text


def test_correlation_id_is_request_id(client: TestClient) -> None:
    response = client.get("/not-found", headers={"X-Request-ID": "req-s12-t1-0001"})
    assert response.headers["X-Request-ID"] == "req-s12-t1-0001"
    body = json.loads(response.text)["error"]
    assert body["correlation_id"] == "req-s12-t1-0001"
    assert body["error_report"]["request_id"] == "req-s12-t1-0001"


def test_input_error_rejects_internal_code() -> None:
    with pytest.raises(ValueError, match="INTERNAL_ERROR"):
        InputError(ErrorCode.INTERNAL_ERROR, "sku")
