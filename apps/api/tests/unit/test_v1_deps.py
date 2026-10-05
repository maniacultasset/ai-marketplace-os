"""S3 T5 (AC5; D9): bearer presence, Idempotency-Key and If-Match header rules."""

from __future__ import annotations

import pytest
from app.api.errors import ErrorEnvelope, install_error_handlers
from app.api.v1.deps import IdempotencyKey, IfMatch, bearer_required
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from mkos_schemas.codes import ErrorCode

ETAG = '"0b1c2d3e-4f50-4617-8899-aabbccddeeff"'


def _app() -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/auth", dependencies=[Depends(bearer_required)])
    async def auth() -> dict[str, str]:
        return {"ok": "yes"}

    @app.post("/idem")
    async def idem(key: IdempotencyKey) -> dict[str, str]:
        return {"key": key}

    @app.patch("/match")
    async def match(etag: IfMatch) -> dict[str, str]:
        return {"etag": etag}

    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(_app(), raise_server_exceptions=False)


@pytest.mark.parametrize(
    "value", [None, "Basic x", "Bearer", "Bearer ", "bearerx abc", "Token abc"]
)
def test_bearer_missing_or_wrong_scheme_401(client: TestClient, value: str | None) -> None:
    headers = {} if value is None else {"Authorization": value}
    response = client.get("/auth", headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    body = response.json()
    ErrorEnvelope.model_validate(body)
    assert body["error"]["type"] == ErrorCode.UNAUTHENTICATED.value


@pytest.mark.parametrize("value", ["Bearer abc", "bearer abc"])
def test_bearer_present_passes(client: TestClient, value: str) -> None:
    assert client.get("/auth", headers={"Authorization": value}).status_code == 200


@pytest.mark.parametrize(
    "key", ["a", "k" * 255, "abc-123_~!", "550e8400-e29b-41d4-a716-446655440000"]
)
def test_idempotency_key_accepted(client: TestClient, key: str) -> None:
    response = client.post("/idem", headers={"Idempotency-Key": key})
    assert response.status_code == 200
    assert response.json() == {"key": key}


@pytest.mark.parametrize("key", [None, "", "k" * 256, "a b", "a\tb"])
def test_idempotency_key_rejected(client: TestClient, key: str | None) -> None:
    headers = {} if key is None else {"Idempotency-Key": key}
    response = client.post("/idem", headers=headers)
    assert response.status_code == 422
    assert response.json()["error"]["type"] == ErrorCode.VALIDATION_FAILED.value


def test_idempotency_key_unicode_rejected(client: TestClient) -> None:
    response = client.post("/idem", headers=[(b"idempotency-key", "kéy".encode())])
    assert response.status_code == 422


def test_if_match_accepted(client: TestClient) -> None:
    response = client.patch("/match", headers={"If-Match": ETAG})
    assert response.status_code == 200


@pytest.mark.parametrize(
    "value", [None, ETAG.strip('"'), f"W/{ETAG}", '"*"', '"abc"', f"{ETAG}, {ETAG}"]
)
def test_if_match_rejected(client: TestClient, value: str | None) -> None:
    headers = {} if value is None else {"If-Match": value}
    response = client.patch("/match", headers=headers)
    assert response.status_code == 422
