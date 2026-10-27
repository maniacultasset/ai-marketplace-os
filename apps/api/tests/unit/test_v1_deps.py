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


# ---- DPP-1: require_recent_mfa (the step-up guard) ----

import time  # noqa: E402
import uuid  # noqa: E402

from app.api.v1 import deps as deps_module  # noqa: E402
from app.core.rbac import Principal  # noqa: E402
from mkos_schemas.auth import JWTClaims  # noqa: E402

USER = uuid.uuid4()
TENANT = uuid.uuid4()


def _claims(*, amr: list[str] | None, age: int | None) -> JWTClaims:
    now = int(time.time())
    return JWTClaims(
        sub=USER,
        tid=TENANT,
        roles=["owner"],
        email="owner@acme.test",
        typ="access",
        jti="j",
        iss="http://localhost:8000",
        aud="ai-marketplace-os",
        iat=now,
        exp=now + 600,
        amr=amr,
        mfa_at=None if age is None else now - age,
        sid=uuid.uuid4(),
    )


def _guard_app(
    monkeypatch: pytest.MonkeyPatch,
    *,
    enforcement: str,
    claims: JWTClaims,
    otp_only: bool,
    has_factor: bool = True,
) -> TestClient:
    from app.core import config

    class _Settings:
        mfa_enforcement = enforcement

    monkeypatch.setattr(deps_module, "get_settings", lambda: _Settings())

    async def factor(_: uuid.UUID) -> bool:
        return has_factor

    monkeypatch.setattr(deps_module, "has_active_factor", factor)
    app = FastAPI()
    install_error_handlers(app)
    guard = deps_module.require_recent_mfa(otp_only=otp_only)

    @app.get("/guarded", dependencies=[guard])
    async def guarded() -> dict[str, str]:
        return {"ok": "yes"}

    app.dependency_overrides[deps_module.access_claims] = lambda: claims
    app.dependency_overrides[deps_module.get_current_principal] = lambda: Principal(
        USER, TENANT, "owner", "owner@acme.test", "j"
    )
    assert config  # the real settings are never read here
    return TestClient(app, raise_server_exceptions=False)


def _status(client: TestClient) -> tuple[int, str | None]:
    response = client.get("/guarded")
    if response.status_code == 200:
        return 200, None
    return response.status_code, response.json()["error"]["type"]


def test_require_recent_mfa_off_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _guard_app(
        monkeypatch, enforcement="off", claims=_claims(amr=None, age=None), otp_only=True
    )
    assert _status(client) == (200, None)


def test_fresh_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    claims = _claims(amr=["pwd", "otp"], age=30)
    assert _status(_guard_app(monkeypatch, enforcement="all", claims=claims, otp_only=True)) == (
        200,
        None,
    )


def test_stale_403_step_up(monkeypatch: pytest.MonkeyPatch) -> None:
    claims = _claims(amr=["pwd", "otp"], age=601)
    client = _guard_app(monkeypatch, enforcement="all", claims=claims, otp_only=False)
    assert _status(client) == (403, "MFA_STEP_UP_REQUIRED")
    pre_mfa = _guard_app(
        monkeypatch, enforcement="all", claims=_claims(amr=None, age=None), otp_only=False
    )
    assert _status(pre_mfa) == (403, "MFA_STEP_UP_REQUIRED")


def test_no_factor_403_enrollment(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _guard_app(
        monkeypatch,
        enforcement="all",
        claims=_claims(amr=["pwd"], age=None),
        otp_only=False,
        has_factor=False,
    )
    assert _status(client) == (403, "MFA_ENROLLMENT_REQUIRED")


def test_otp_only_refuses_rc(monkeypatch: pytest.MonkeyPatch) -> None:
    claims = _claims(amr=["pwd", "rc"], age=10)
    client = _guard_app(monkeypatch, enforcement="all", claims=claims, otp_only=True)
    assert _status(client) == (403, "MFA_STEP_UP_REQUIRED")


def test_any_accepts_rc(monkeypatch: pytest.MonkeyPatch) -> None:
    claims = _claims(amr=["pwd", "rc"], age=10)
    client = _guard_app(monkeypatch, enforcement="all", claims=claims, otp_only=False)
    assert _status(client) == (200, None)


def test_step_up_guard_is_cached_and_marked() -> None:
    first = deps_module.require_recent_mfa(otp_only=True)
    assert first is deps_module.require_recent_mfa(otp_only=True)
    assert getattr(first.dependency, deps_module.STEP_UP_MARK) == "otp"
    assert (
        getattr(deps_module.require_recent_mfa(otp_only=False).dependency, deps_module.STEP_UP_MARK)
        == "any"
    )
