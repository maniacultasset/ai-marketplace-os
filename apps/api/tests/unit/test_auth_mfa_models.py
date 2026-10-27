"""DPP-1 T11: the MFA, step-up and password request and response models."""

from __future__ import annotations

from typing import Any

import pytest
from app.api.v1.schemas.auth import (
    LoginConfirmRequest,
    LoginMfaRequest,
    LoginResult,
    PasswordChangeRequest,
    PasswordExpiredRequest,
    StepUpRequest,
    TotpEnrollRequest,
)
from pydantic import ValidationError

TOKENS: dict[str, Any] = {
    "access_token": "a.b.c",
    "refresh_token": "r.s.t",
    "token_type": "bearer",
    "expires_in": 1800,
    "memberships": [],
}


ARABIC_INDIC = "".join(chr(0x0661 + i) for i in range(6))
FULL_WIDTH = "".join(chr(0xFF11 + i) for i in range(6))


# A blank around the code is stripped by the strict base model, so " 123456" is accepted.
@pytest.mark.parametrize("code", ["12345", "1234567", "12345a", ARABIC_INDIC, FULL_WIDTH])
def test_code_ascii_digits_only(code: str) -> None:
    with pytest.raises(ValidationError):
        LoginConfirmRequest.model_validate({"challenge_token": "ch", "code": code})
    with pytest.raises(ValidationError):
        StepUpRequest.model_validate({"code": code})
    assert LoginConfirmRequest.model_validate({"challenge_token": "ch", "code": "012345"}).code


@pytest.mark.parametrize(
    "body",
    [
        {"challenge_token": "ch"},
        {"challenge_token": "ch", "code": "123456", "recovery_code": "abcd"},
    ],
)
def test_exactly_one_of_code_or_recovery(body: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        LoginMfaRequest.model_validate(body)
    step = {k: v for k, v in body.items() if k != "challenge_token"}
    with pytest.raises(ValidationError):
        StepUpRequest.model_validate(step)
    LoginMfaRequest.model_validate({"challenge_token": "ch", "recovery_code": "abcd-efgh"})
    StepUpRequest.model_validate({"code": "123456"})


def test_login_result_signed_in_requires_tokens() -> None:
    LoginResult.model_validate({"status": "signed_in", **TOKENS})
    with pytest.raises(ValidationError):
        LoginResult.model_validate({"status": "signed_in"})
    with pytest.raises(ValidationError):
        LoginResult.model_validate({"status": "signed_in", **TOKENS, "challenge_token": "ch"})


@pytest.mark.parametrize(
    "status", ["mfa_required", "mfa_enrollment_required", "password_change_required"]
)
def test_login_result_challenge_has_no_tokens(status: str) -> None:
    LoginResult.model_validate(
        {"status": status, "challenge_token": "ch", "challenge_expires_in": 300}
    )
    with pytest.raises(ValidationError):
        LoginResult.model_validate({"status": status})
    with pytest.raises(ValidationError):
        LoginResult.model_validate({"status": status, "challenge_token": "ch", **TOKENS})


def test_login_result_status_vocabulary() -> None:
    with pytest.raises(ValidationError):
        LoginResult.model_validate({"status": "locked", "challenge_token": "ch"})


def test_password_fields_are_secret_in_repr() -> None:
    change = PasswordChangeRequest.model_validate(
        {"current_password": "Old-Secret-0123", "new_password": "New-Secret-4567"}
    )
    expired = PasswordExpiredRequest.model_validate(
        {"challenge_token": "challenge-canary", "new_password": "New-Secret-4567"}
    )
    enroll = TotpEnrollRequest.model_validate({"current_password": "Old-Secret-0123"})
    mfa = LoginMfaRequest.model_validate(
        {"challenge_token": "challenge-canary", "recovery_code": "rc-canary"}
    )
    for model in (change, expired, enroll, mfa):
        text = repr(model) + str(model)
        for canary in ("Old-Secret-0123", "New-Secret-4567", "challenge-canary", "rc-canary"):
            assert canary not in text


def test_challenge_token_max_len() -> None:
    LoginConfirmRequest.model_validate({"challenge_token": "x" * 256, "code": "123456"})
    with pytest.raises(ValidationError):
        LoginConfirmRequest.model_validate({"challenge_token": "x" * 257, "code": "123456"})
    with pytest.raises(ValidationError):
        LoginConfirmRequest.model_validate({"challenge_token": "", "code": "123456"})


def test_recovery_code_max_len() -> None:
    with pytest.raises(ValidationError):
        StepUpRequest.model_validate({"recovery_code": "a" * 65})


def test_new_password_bounds() -> None:
    with pytest.raises(ValidationError):
        PasswordChangeRequest.model_validate({"current_password": "x", "new_password": ""})
    with pytest.raises(ValidationError):
        PasswordChangeRequest.model_validate({"current_password": "x", "new_password": "x" * 1025})


def test_requests_are_strict() -> None:
    with pytest.raises(ValidationError):
        StepUpRequest.model_validate({"code": "123456", "remember_device": True})
