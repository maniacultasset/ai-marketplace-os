"""Team invites by copied link (SI5, ADR-0029): create, list, revoke, and the public lookup and
accept steps, plus `join` for a signed-in person whose email matches.

The raw token is returned once, inside `invite_url` (`<dashboard>/invite#token=<token>`); only its
sha256 is stored. A token is 43 base64url characters (32 random bytes, no padding), so anything
else is a 422 before any lookup. Every refusal of a well-formed token is one generic 404.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from mkos_schemas.base import NonBlankStr, StrictModel, WireModel
from pydantic import AfterValidator, BeforeValidator, Field, model_validator
from pydantic_core import PydanticCustomError

from app.api.pagination import Page
from app.api.v1.schemas.auth import Email, NewPassword
from app.api.v1.schemas.google import Binding, FlowHandle
from app.api.v1.schemas.signup import SignupEmail

InviteRole = Literal["owner", "admin", "operator", "reviewer"]
InviteStatus = Literal["pending", "accepted", "revoked", "expired"]
DISPLAY_NAME_MAX = 120

InviteToken = Annotated[
    str,
    Field(
        pattern=r"^[A-Za-z0-9_-]{43}$",
        description="The `token` from the link's `#token=` fragment (43 base64url characters). "
        "Never log it or send it to analytics.",
    ),
]


def _trim(value: object) -> object:
    return value.strip() if isinstance(value, str) else value


def _no_nul(value: str) -> str:
    if "\x00" in value:
        raise PydanticCustomError("mkos_invalid", "Text must not contain NUL.")
    return value


# Trimmed before the pattern, then lower-cased within `ck_invitations_email` (the sign-up rule).
InviteEmailIn = Annotated[SignupEmail, BeforeValidator(_trim)]
DisplayName = Annotated[NonBlankStr, Field(max_length=DISPLAY_NAME_MAX), AfterValidator(_no_nul)]


class InviteCreate(StrictModel):
    email: InviteEmailIn
    role: InviteRole


class InviteOut(WireModel):
    id: UUID
    email: Email
    role: InviteRole
    status: InviteStatus = Field(
        description="Derived: `accepted`, `revoked`, `expired` (past `expires_at`), else `pending`."
    )
    invited_by_user_id: UUID | None = None
    expires_at: datetime
    created_at: datetime
    accepted_at: datetime | None = None
    revoked_at: datetime | None = None


class InviteCreated(WireModel):
    invite: InviteOut
    invite_url: str = Field(
        description="`<dashboard>/invite#token=<token>`, shown once: copy it to the person. It "
        "works once, for 7 days; a new invite for the same email replaces it."
    )


class InvitePage(Page[InviteOut]):
    pass


class InviteTokenIn(StrictModel):
    token: InviteToken


class InvitePreview(WireModel):
    tenant_name: str
    role: InviteRole
    email: Email
    expires_at: datetime
    account: Literal["new", "existing"] = Field(
        description="`existing`: the email has a Kuinn account that can sign in, so sign in and "
        "call `invites_join`; `new`: accept with `invites_accept` (an account that cannot sign "
        "in yet, such as a pending sign-up, gives its own password)."
    )


class GoogleProof(StrictModel):
    """The `needs_details` handle of `auth_google_complete`, and the browser's binding."""

    handle: FlowHandle
    binding: Binding


class InviteAccept(StrictModel):
    """A new account for the invited email: exactly one of `password` and `google`."""

    token: InviteToken
    display_name: DisplayName | None = None
    password: NewPassword | None = None
    google: GoogleProof | None = None

    @model_validator(mode="after")
    def _one_credential(self) -> InviteAccept:
        if (self.password is None) == (self.google is None):
            raise PydanticCustomError("mkos_invalid", "Give exactly one of password and google.")
        return self


class InviteAccepted(WireModel):
    email: Email
    tenant_name: str


class InviteJoined(WireModel):
    tenant_id: UUID
    role: InviteRole


class ResetLinkOut(WireModel):
    reset_url: str = Field(
        description="`<dashboard>/reset-password#token=<token>`, shown once: copy it to the "
        "member. It works once, for 24 hours; a new link replaces it."
    )
    expires_at: datetime


class PasswordResetIn(StrictModel):
    token: InviteToken
    new_password: NewPassword
