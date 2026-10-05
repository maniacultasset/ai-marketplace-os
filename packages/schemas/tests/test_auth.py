"""JWTClaims (ADR-0006)."""

from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError

from mkos_schemas.auth import JWTClaims

_SUB = UUID("11111111-1111-1111-1111-111111111111")
_TID = UUID("22222222-2222-2222-2222-222222222222")


def _claims(**overrides: object) -> JWTClaims:
    base = dict(
        sub=_SUB,
        tid=_TID,
        roles=["owner"],
        email="u@example.com",
        typ="access",
        jti="jti-1",
        iss="https://api.mkos.internal",
        aud="mkos",
        iat=1_700_000_000,
        exp=1_700_000_600,
    )
    base.update(overrides)
    return JWTClaims.model_validate(base)


def test_jwt_claims_fields() -> None:
    expected = {
        "sub",
        "tid",
        "roles",
        "email",
        "typ",
        "jti",
        "iss",
        "aud",
        "iat",
        "exp",
    }
    assert set(JWTClaims.model_fields) == expected


def test_jwt_claims_roles_adr_0006() -> None:
    _claims(roles=["owner", "operator"])
    with pytest.raises(ValidationError):
        _claims(roles=["guest"])
    with pytest.raises(ValidationError):
        _claims(roles=[])
    with pytest.raises(ValidationError):
        _claims(roles=["owner", "owner"])


def test_jwt_claims_is_strict() -> None:
    with pytest.raises(ValidationError):
        _claims(extra="x")
    with pytest.raises(ValidationError):
        JWTClaims.model_validate({**_claims().model_dump(), "tenant_id": str(_TID)})


def test_jwt_claims_exp_after_iat() -> None:
    with pytest.raises(ValidationError):
        _claims(exp=1_700_000_000, iat=1_700_000_600)


def test_jwt_claims_typ_vocabulary() -> None:
    _claims(typ="refresh")
    with pytest.raises(ValidationError):
        _claims(typ="id")
