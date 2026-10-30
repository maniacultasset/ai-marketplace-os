"""Auth and members request and response models (owner step S10), with the DPP-1 MFA, lockout and
password models (contract 2.0.0, ADR-0021) and the trusted browsers (contract 2.1.0, ADR-0023)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from mkos_schemas.auth import Role
from mkos_schemas.base import NonBlankStr, StrictModel, WireModel
from pydantic import AfterValidator, Field, SecretStr, StrictBool, model_validator
from pydantic_core import PydanticCustomError

from app.api.pagination import Page

Email = Annotated[str, Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+$")]
_VISIBLE_ASCII = frozenset(chr(c) for c in range(0x21, 0x7F))


def _ascii_email(value: str) -> str:
    if not set(value) <= _VISIBLE_ASCII:
        raise PydanticCustomError("mkos_invalid", "Email must be plain ASCII.")
    return value


# A login email is visible ASCII only, so a Unicode look-alike of a real address is a 422, never
# a lookup miss that looks like a wrong password (CODE-PLAN S10 C8). A validator, not a new
# pattern: the declared contract stays as S3 froze it.
LoginEmail = Annotated[Email, AfterValidator(_ascii_email)]
RefreshToken = Annotated[SecretStr, Field(min_length=1, max_length=4096)]
# ADR-0023: the trusted-browser token exactly as the server returned it (43 base64url characters).
# Any other value is not refused: it simply matches nothing (`device_token_invalid`), so a
# damaged browser entry never blocks a sign-in.
DeviceToken = Annotated[
    SecretStr,
    Field(
        min_length=1,
        max_length=128,
        description="The `device_token` this browser was given for this account (send it in "
        "the body only; keep it in localStorage).",
    ),
]


class LoginRequest(StrictModel):
    email: LoginEmail
    password: Annotated[SecretStr, Field(min_length=1, max_length=1024)]
    device_token: DeviceToken | None = Field(
        default=None,
        description="ADR-0023: this browser's trusted-browser token for this email, if any. "
        "Read only after the password is correct; when it is live the code step is skipped.",
    )


class RefreshRequest(StrictModel):
    refresh_token: RefreshToken


class LogoutRequest(StrictModel):
    refresh_token: RefreshToken


class MembershipOut(WireModel):
    tenant_id: UUID
    tenant_name: NonBlankStr
    role: Role


class TokenResponse(WireModel):
    access_token: NonBlankStr
    refresh_token: NonBlankStr
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 - the OAuth token type, not a secret
    expires_in: Annotated[int, Field(ge=1, description="Access token lifetime in seconds.")]
    memberships: list[MembershipOut]


# ---- DPP-1: the login union, MFA and passwords ----

SignInStatus = Literal[
    "signed_in", "mfa_required", "mfa_enrollment_required", "password_change_required"
]
# ASCII digits only: Pydantic's `\d` would also match Arabic-Indic and full-width digits.
Code = Annotated[str, Field(pattern=r"^[0-9]{6}$", description="A 6-digit TOTP code.")]
RecoveryCode = Annotated[
    SecretStr,
    Field(
        min_length=1,
        max_length=64,
        description="A recovery code; case, spaces and dashes " "are ignored.",
    ),
]
ChallengeToken = Annotated[
    SecretStr,
    Field(
        min_length=1,
        max_length=256,
        description="The opaque `challenge_token` of the last "
        "sign-in step (keep it in memory only).",
    ),
]
NewPassword = Annotated[
    SecretStr,
    Field(
        min_length=1,
        max_length=1024,
        description="12+ characters with an upper-case letter, a lower-case letter, a digit and "
        "a symbol; not the name or the first part of the email; none of the last 10.",
    ),
]
CurrentPassword = Annotated[SecretStr, Field(min_length=1, max_length=1024)]


class LoginResult(WireModel):
    """`signed_in` carries today's token fields; every other status carries a challenge for the
    next step instead (and no token)."""

    status: SignInStatus = "signed_in"
    access_token: NonBlankStr | None = None
    refresh_token: NonBlankStr | None = None
    token_type: Literal["bearer"] | None = None
    expires_in: (
        Annotated[int, Field(ge=1, description="Access token lifetime in seconds.")] | None
    ) = None
    memberships: list[MembershipOut] | None = None
    challenge_token: NonBlankStr | None = Field(
        default=None, description="Send it to the next step's route; keep it in memory only."
    )
    challenge_expires_in: Annotated[int, Field(ge=1)] | None = Field(
        default=None, description="Seconds the challenge stays valid (900 to enrol, else 300)."
    )
    mfa_grace_until: datetime | None = Field(
        default=None,
        description="Set while the user may still sign in without a factor: show a banner.",
    )
    device_token: NonBlankStr | None = Field(
        default=None,
        description="ADR-0023, `signed_in` only: a new trusted-browser token for this email "
        "(after `trust_device`, or the next value of the one sent). Replace the stored one; "
        "the old value stops working.",
    )
    device_expires_at: datetime | None = Field(
        default=None,
        description="With `device_token`: when this browser stops being trusted (fixed; a use "
        "never moves it).",
    )
    device_token_invalid: bool | None = Field(
        default=None,
        description="ADR-0023: true when a device token was sent, the first factor passed, "
        "and it names no trusted browser of this account: forget the stored token.",
    )
    device_trust_days: Annotated[int, Field(ge=1, le=30)] | None = Field(
        default=None,
        description="ADR-0023: on the code steps, how many days \"Don't ask for a code on "
        'this browser" lasts (30, or 7 for a platform admin).',
    )

    @model_validator(mode="after")
    def _tokens_or_challenge(self) -> LoginResult:
        tokens = (self.access_token, self.refresh_token, self.expires_in, self.memberships)
        has_tokens = all(t is not None for t in tokens)
        if self.status == "signed_in":
            if not has_tokens or self.challenge_token is not None:
                raise ValueError("signed_in carries the tokens and no challenge")
        elif any(t is not None for t in tokens) or self.challenge_token is None:
            raise ValueError("a challenge status carries a challenge and no tokens")
        if (self.device_token is None) != (self.device_expires_at is None):
            raise ValueError("device_token and device_expires_at come together")
        if self.device_token is not None and self.status != "signed_in":
            raise ValueError("a device token comes only with signed_in")
        return self


def _one_of_code(code: object, recovery_code: object) -> None:
    if (code is None) == (recovery_code is None):
        raise PydanticCustomError("mkos_invalid", "Give exactly one of code or recovery_code.")


TrustDevice = Annotated[
    StrictBool,
    Field(
        description="ADR-0023: trust this browser after this code (30 days, 7 for a platform "
        "admin): the next sign-in here skips the code. Ignored with a recovery code."
    ),
]


class LoginMfaRequest(StrictModel):
    challenge_token: ChallengeToken
    code: Code | None = None
    recovery_code: RecoveryCode | None = None
    trust_device: TrustDevice = False

    @model_validator(mode="after")
    def _exactly_one(self) -> LoginMfaRequest:
        _one_of_code(self.code, self.recovery_code)
        return self


class LoginEnrollRequest(StrictModel):
    challenge_token: ChallengeToken


class LoginConfirmRequest(StrictModel):
    challenge_token: ChallengeToken
    code: Code
    trust_device: TrustDevice = False


class TotpEnrollRequest(StrictModel):
    """`current_password` is required while the account has no factor; replacing a factor
    needs a step-up in the last 30 minutes instead."""

    current_password: CurrentPassword | None = None


class TotpConfirmRequest(StrictModel):
    code: Code


class StepUpRequest(StrictModel):
    code: Code | None = None
    recovery_code: RecoveryCode | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> StepUpRequest:
        _one_of_code(self.code, self.recovery_code)
        return self


class PasswordChangeRequest(StrictModel):
    current_password: CurrentPassword
    new_password: NewPassword


class PasswordExpiredRequest(StrictModel):
    challenge_token: ChallengeToken
    new_password: NewPassword


RecoveryCodes = Annotated[
    list[NonBlankStr],
    Field(min_length=10, max_length=10, description="Shown once: the user must save them."),
]


class TotpEnrollOut(WireModel):
    otpauth_uri: NonBlankStr = Field(description="Render as a QR code on the client.")
    secret_b32: NonBlankStr = Field(description="The same secret for manual entry.")


class LoginConfirmOut(WireModel):
    recovery_codes: RecoveryCodes
    result: LoginResult


class TotpConfirmOut(WireModel):
    recovery_codes: RecoveryCodes


class RecoveryCodesOut(WireModel):
    recovery_codes: RecoveryCodes


class StepUpOut(WireModel):
    """A new access token only: swap it in and keep the stored refresh token."""

    access_token: NonBlankStr
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 - the OAuth token type, not a secret
    expires_in: Annotated[int, Field(ge=1)]


class SecurityOut(WireModel):
    mfa_enrolled: bool
    recovery_codes_left: Annotated[int, Field(ge=0, le=10)]
    enforcement: Literal["off", "all"]
    mfa_grace_until: datetime | None = None
    password_changed_at: datetime | None = None
    password_expires_at: datetime | None = None
    has_password: bool | None = Field(
        default=None,
        description='False for an account created with Google (ADR-0020): hide "Change '
        'password" and the password dates.',
    )
    google_linked: bool | None = Field(
        default=None,
        description="True when a Google account can sign in to this account (ADR-0020); "
        "`POST /auth/google/unlink` removes it.",
    )


class TrustedDeviceOut(WireModel):
    """One trusted browser (ADR-0023). `label` is a coarse summary of its browser and system."""

    id: UUID
    label: NonBlankStr = Field(description='E.g. "Chrome on macOS".')
    created_at: datetime
    last_used_at: datetime | None = Field(
        default=None, description="The last sign-in that skipped the code here."
    )
    expires_at: datetime
    current: bool = Field(description="True for the browser this session signed in on.")


class TrustedDevicesOut(WireModel):
    devices: Annotated[list[TrustedDeviceOut], Field(max_length=10)] = Field(
        description="The live trusted browsers, newest first (at most 10)."
    )


class MeOut(WireModel):
    user_id: UUID
    email: Email
    display_name: str | None = None
    tenant_id: UUID
    role: Role
    memberships: list[MembershipOut]
    is_platform_admin: bool = Field(
        default=False,
        description="True for a platform admin (ADR-0019): show the sign-up request queue.",
    )


class SwitchTenantRequest(StrictModel):
    """`target_tenant_id`, not `tenant_id`: the one body field naming a tenant is the switch
    target, checked against the caller's own active memberships (ADR-0006; CODE-PLAN S10 Q9)."""

    target_tenant_id: UUID


class MemberOut(WireModel):
    user_id: UUID
    email: Email
    display_name: str | None = None
    role: Role
    is_active: bool


class MemberPage(Page[MemberOut]):
    pass


class MemberPatch(StrictModel):
    """At least one of `role` and `is_active`."""

    role: Role | None = None
    is_active: StrictBool | None = None

    @model_validator(mode="after")
    def _one_change(self) -> MemberPatch:
        if self.role is None and self.is_active is None:
            raise PydanticCustomError("mkos_invalid", "Give role or is_active.")
        return self
