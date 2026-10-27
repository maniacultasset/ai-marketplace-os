"""S10 C1 (AC11): JWT encode/decode pinned to HS256 with iss, aud and exp required (ADR-0005:30)."""

from __future__ import annotations

import base64
import json
import re
import time
import uuid
from typing import Any

import jwt
import pytest
from mkos_schemas.auth import JWTClaims
from tests.conftest import CI_JWT_SECRET, REPO_ROOT

pytestmark = pytest.mark.usefixtures("ci_env")

ISSUER = "http://localhost:8000"  # the Settings defaults the CI env block keeps
AUDIENCE = "ai-marketplace-os"


def _claims(**overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": str(uuid.uuid4()),
        "tid": str(uuid.uuid4()),
        "roles": ["owner"],
        "email": "owner@acme.test",
        "typ": "access",
        "jti": str(uuid.uuid4()),
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + 600,
    }
    claims.update(overrides)
    return claims


def _sign(payload: dict[str, Any], *, secret: str = CI_JWT_SECRET, alg: str = "HS256") -> str:
    return jwt.encode(payload, secret, algorithm=alg)


def _b64(part: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(part).encode()).rstrip(b"=").decode()


def _rejects(token: str, typ: str = "access") -> None:
    from app.core.security import InvalidToken, decode

    with pytest.raises(InvalidToken) as caught:
        decode(token, typ=typ)  # type: ignore[arg-type]
    # The error never names the claim, the token or the secret.
    assert str(caught.value) == "invalid token"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_jwt_round_trip_valid_claims() -> None:
    from app.core.security import decode, encode

    claims = JWTClaims.model_validate(_claims())
    token = encode(claims)
    assert decode(token, typ="access") == claims
    header = jwt.get_unverified_header(token)
    assert header == {"alg": "HS256", "typ": "JWT"}


def test_jwt_rejects_alg_none() -> None:
    unsigned = f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64(_claims())}."
    _rejects(unsigned)


def test_jwt_rejects_other_secret() -> None:
    _rejects(_sign(_claims(), secret="another_secret_that_is_at_least_32_chars"))


def test_jwt_rejects_wrong_iss() -> None:
    _rejects(_sign(_claims(iss="https://evil.example")))


def test_jwt_rejects_wrong_aud() -> None:
    _rejects(_sign(_claims(aud="someone-else")))


def test_jwt_rejects_expired() -> None:
    now = int(time.time())
    _rejects(_sign(_claims(iat=now - 3600, exp=now - 60)))


def test_jwt_rejects_refresh_as_access() -> None:
    from app.core.security import decode

    refresh = _sign(_claims(typ="refresh"))
    _rejects(refresh, typ="access")
    assert decode(refresh, typ="refresh").typ == "refresh"
    _rejects(_sign(_claims(typ="access")), typ="refresh")


@pytest.mark.parametrize("claim", ["exp", "iss", "aud", "iat", "sub", "tid", "jti", "typ"])
def test_jwt_rejects_missing_claim(claim: str) -> None:
    payload = _claims()
    del payload[claim]
    _rejects(_sign(payload))


def test_jwt_rejects_extra_claim() -> None:
    # JWTClaims is strict: an unknown claim (a smuggled `role` or `tenant_id`) is refused.
    _rejects(_sign(_claims(tenant_id=str(uuid.uuid4()))))


def test_jwt_rejects_hs512() -> None:
    _rejects(_sign(_claims(), alg="HS512"))


@pytest.mark.parametrize(
    "token", ["", "not-a-jwt", "a.b.c", "a.b", "....", "Bearer x", "é.é.é", "x" * 5000]
)
def test_jwt_rejects_malformed(token: str) -> None:
    _rejects(token)


def test_jwt_rejects_bad_signature() -> None:
    token = _sign(_claims())
    head, body, signature = token.split(".")
    flipped = ("A" if signature[0] != "A" else "B") + signature[1:]
    _rejects(f"{head}.{body}.{flipped}")


def test_jwt_rejects_tampered_payload() -> None:
    token = _sign(_claims(roles=["reviewer"]))
    head, _, signature = token.split(".")
    _rejects(f"{head}.{_b64(_claims(roles=['owner']))}.{signature}")


def test_new_jti_unique() -> None:
    from app.core.security import new_jti

    values = {new_jti() for _ in range(200)}
    assert len(values) == 200
    assert all(0 < len(v) <= 128 for v in values)


def test_token_hash_32_bytes() -> None:
    from app.core.security import token_hash

    digest = token_hash("abc")
    assert isinstance(digest, bytes)
    assert len(digest) == 32
    assert digest == token_hash("abc")
    assert digest != token_hash("abd")


def test_issue_sets_ttl_from_settings() -> None:
    from app.core.security import decode, encode, issue_claims

    sub, tid = uuid.uuid4(), uuid.uuid4()
    access = issue_claims(sub=sub, tid=tid, role="reviewer", email="r@acme.test", typ="access")
    assert access.exp - access.iat == 30 * 60
    assert (access.iss, access.aud, access.roles) == (ISSUER, AUDIENCE, ["reviewer"])
    refresh = issue_claims(sub=sub, tid=tid, role="reviewer", email="r@acme.test", typ="refresh")
    assert refresh.exp - refresh.iat == 14 * 24 * 3600
    assert refresh.jti != access.jti
    assert decode(encode(refresh), typ="refresh") == refresh


def test_lock_pins_pyjwt() -> None:
    pyproject = (REPO_ROOT / "apps/api/pyproject.toml").read_text(encoding="utf-8")
    assert '"pyjwt[crypto]>=2.15,<3",' in pyproject
    lock = (REPO_ROOT / "uv.lock").read_text(encoding="utf-8")
    found = re.search(r'\[\[package\]\]\nname = "pyjwt"\nversion = "(\d+)\.(\d+)\.(\d+)"', lock)
    assert found is not None
    major, minor = int(found.group(1)), int(found.group(2))
    assert major == 2 and minor >= 15
    assert '\nname = "cryptography"\n' in lock
    for banned in ('name = "python-jose"', 'name = "passlib"', 'name = "authlib"'):
        assert banned not in lock


# ---- DPP-1 (contract 2.0.0): the optional amr, mfa_at and sid claims ----

FROZEN_CLAIMS = {"sub", "tid", "roles", "email", "typ", "jti", "iss", "aud", "iat", "exp"}


def _raw_payload(token: str) -> dict[str, Any]:
    return jwt.decode(token, options={"verify_signature": False})


def test_off_tokens_carry_no_new_keys() -> None:
    from app.core.security import encode, issue_claims

    claims = issue_claims(
        sub=uuid.uuid4(), tid=uuid.uuid4(), role="owner", email="o@acme.test", typ="access"
    )
    assert set(_raw_payload(encode(claims))) == FROZEN_CLAIMS


def test_all_tokens_round_trip_amr_mfa_at_sid() -> None:
    from app.core.security import decode, encode, issue_claims

    sid = uuid.uuid4()
    now = int(time.time())
    claims = issue_claims(
        sub=uuid.uuid4(),
        tid=uuid.uuid4(),
        role="owner",
        email="o@acme.test",
        typ="access",
        amr=["pwd", "otp"],
        mfa_at=now - 5,
        sid=sid,
    )
    token = encode(claims)
    assert set(_raw_payload(token)) == FROZEN_CLAIMS | {"amr", "mfa_at", "sid"}
    decoded = decode(token, typ="access")
    assert (decoded.amr, decoded.mfa_at, decoded.sid) == (["pwd", "otp"], now - 5, sid)


def test_mfa_at_is_never_after_iat() -> None:
    from app.core.security import issue_claims

    claims = issue_claims(
        sub=uuid.uuid4(),
        tid=uuid.uuid4(),
        role="owner",
        email="o@acme.test",
        typ="refresh",
        amr=["pwd", "rc"],
        mfa_at=int(time.time()) + 60,
    )
    assert claims.mfa_at is not None and claims.mfa_at <= claims.iat
