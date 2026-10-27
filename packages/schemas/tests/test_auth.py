"""JWTClaims (ADR-0006)."""

from __future__ import annotations

from uuid import UUID

import pytest
from mkos_schemas.auth import JWTClaims
from pydantic import ValidationError

_SUB = UUID("11111111-1111-1111-1111-111111111111")
_TID = UUID("22222222-2222-2222-2222-222222222222")


def _claims(**overrides: object) -> JWTClaims:
    base = {
        "sub": _SUB,
        "tid": _TID,
        "roles": ["owner"],
        "email": "u@example.com",
        "typ": "access",
        "jti": "jti-1",
        "iss": "https://api.mkos.internal",
        "aud": "mkos",
        "iat": 1_700_000_000,
        "exp": 1_700_000_600,
    }
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
        "amr",
        "mfa_at",
        "sid",
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


# ---- 2.0.0 (DPP-1, ADR-0021): amr, mfa_at, sid ----


def test_1_2_0_token_shape_still_validates() -> None:
    claims = _claims()
    assert (claims.amr, claims.mfa_at, claims.sid) == (None, None, None)
    dumped = claims.model_dump(mode="json", exclude_none=True)
    assert set(dumped) == {"sub", "tid", "roles", "email", "typ", "jti", "iss", "aud", "iat", "exp"}


def test_new_claims_round_trip() -> None:
    sid = UUID("33333333-3333-3333-3333-333333333333")
    claims = _claims(amr=["pwd", "otp"], mfa_at=1_700_000_000, sid=str(sid))
    assert claims.amr == ["pwd", "otp"] and claims.sid == sid
    assert JWTClaims.model_validate(claims.model_dump(mode="json")) == claims


@pytest.mark.parametrize("amr", [["sms"], [""], ["PWD"], [], ["pwd", "otp", "rc", "pwd"]])
def test_amr_unknown_value_refused(amr: list[str]) -> None:
    with pytest.raises(ValidationError):
        _claims(amr=amr)


def test_amr_duplicates_refused() -> None:
    with pytest.raises(ValidationError):
        _claims(amr=["pwd", "pwd"])


def test_mfa_at_after_iat_refused() -> None:
    _claims(mfa_at=1_700_000_000)
    with pytest.raises(ValidationError):
        _claims(mfa_at=1_700_000_001)
    with pytest.raises(ValidationError):
        _claims(mfa_at=-1)


@pytest.mark.parametrize("sid", ["not-a-uuid", "", 7])
def test_sid_must_be_uuid(sid: object) -> None:
    with pytest.raises(ValidationError):
        _claims(sid=sid)


def test_extra_claim_still_refused() -> None:
    with pytest.raises(ValidationError):
        _claims(amr=["pwd"], acr="mfa")
