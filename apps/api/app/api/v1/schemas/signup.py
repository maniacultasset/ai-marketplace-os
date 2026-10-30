"""Self-serve sign-up and the platform admin's queue (ADR-0019).

The sign-up form carries the minimum to open an account: brand name, owner name, email,
password, and an optional phone and website. Everything else (tone, guidelines, glossary, brand
documents) is filled in after approval through the existing `PATCH /tenants/me` and
`/brand/documents`. Every request model is strict (`extra="forbid"`), so a body naming
`is_platform_admin`, `status` or a tenant is 422.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Final, Literal
from uuid import UUID

from mkos_schemas.base import NonBlankStr, StrictModel, WireModel
from pydantic import AfterValidator, Field, SecretStr
from pydantic_core import PydanticCustomError

from app.api.pagination import Page
from app.api.v1.schemas.auth import Email, LoginEmail
from app.api.v1.schemas.tenants import ProfileName
from app.core.password_policy import PASSWORD_MAX, PASSWORD_MIN  # the staff CLI's rule

EMAIL_OCTETS_MAX: Final = 254  # ck_users_email
REASON_MAX: Final = 1000
PHONE_PATTERN: Final = r"^\+?[0-9][0-9 ()-]{5,30}$"
WEBSITE_PATTERN: Final = r"^https?://[^\s/?#\x00-\x1f\x7f]+\.[^\s\x00-\x1f\x7f]+$"

SignupStatus = Literal["pending", "approved", "rejected"]


def _signup_email(value: str) -> str:
    """Trimmed and lower-cased, as the staff CLI stores it, within `ck_users_email`."""
    email = value.strip().lower()
    if len(email.encode("utf-8")) > EMAIL_OCTETS_MAX or email.count("@") != 1:
        raise PydanticCustomError("mkos_invalid", "Email must be one plain ASCII address.")
    return email


def _no_nul(value: str) -> str:
    if "\x00" in value:
        raise PydanticCustomError("mkos_invalid", "Text must not contain NUL.")
    return value


SignupEmail = Annotated[LoginEmail, AfterValidator(_signup_email)]
NewPassword = Annotated[SecretStr, Field(min_length=PASSWORD_MIN, max_length=PASSWORD_MAX)]
Phone = Annotated[str, Field(max_length=32, pattern=PHONE_PATTERN)]
Website = Annotated[str, Field(max_length=2048, pattern=WEBSITE_PATTERN), AfterValidator(_no_nul)]
Reason = Annotated[NonBlankStr, Field(max_length=REASON_MAX), AfterValidator(_no_nul)]


class SignupCreate(StrictModel):
    brand_name: ProfileName
    owner_name: ProfileName
    email: SignupEmail
    password: NewPassword
    phone: Phone | None = None
    website: Website | None = None


class SignupAccepted(WireModel):
    """Always this shape and status, so the answer never says whether the email had an
    account: a wrong password for an existing email gets a fresh id that names nothing."""

    request_id: UUID
    status: Literal["pending"]


class SignupStatusRequest(StrictModel):
    email: LoginEmail
    password: Annotated[SecretStr, Field(min_length=1, max_length=PASSWORD_MAX)]


class SignupStatusOut(WireModel):
    request_id: UUID
    brand_name: NonBlankStr
    status: SignupStatus
    reason: str | None = None
    tenant_id: UUID | None = None
    created_at: datetime
    decided_at: datetime | None = None


class SignupRequestOut(WireModel):
    id: UUID
    user_id: UUID
    email: Email
    brand_name: NonBlankStr
    owner_name: NonBlankStr
    phone: str | None = None
    website: str | None = None
    status: SignupStatus
    reason: str | None = None
    tenant_id: UUID | None = Field(
        default=None, description="The tenant created on approve; null until then."
    )
    created_at: datetime
    decided_at: datetime | None = None
    verified_via: Literal["google"] | None = Field(
        default=None,
        description="`google` when Google verified the applicant's email (ADR-0020): show a "
        '"Verified by Google" badge. Null for a password sign-up.',
    )


class SignupRequestPage(Page[SignupRequestOut]):
    pass


class SignupApprove(StrictModel):
    trial_days: Annotated[int, Field(ge=1, le=365)] | None = Field(
        default=None, description="Trial length in days (default 14)."
    )


class SignupReject(StrictModel):
    reason: Reason
