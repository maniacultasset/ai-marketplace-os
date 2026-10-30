"""Sign in and sign up with Google (ADR-0020; contract 2.1.0): the request and response models of
`/auth/google/complete`, `/auth/google/link` and `/signup/google`.

The browser holds a random binding secret (43 base64url characters, kept in sessionStorage); it
sent only its sha256 to `start`, and sends the secret itself with every JSON step. Handles are
opaque, single use and short lived; keep them out of logs and storage. Every request model is
strict (`extra="forbid"`).
"""

from __future__ import annotations

from typing import Annotated, Literal

from mkos_schemas.base import NonBlankStr, StrictModel, WireModel
from pydantic import Field, SecretStr, model_validator
from pydantic_core import PydanticCustomError

from app.api.v1.schemas.auth import DeviceToken, Email, LoginResult
from app.api.v1.schemas.signup import Phone, SignupStatusOut, Website
from app.api.v1.schemas.tenants import ProfileName

GoogleOutcome = Literal["sign_in", "pending", "needs_details", "link_required"]
Binding = Annotated[
    SecretStr,
    Field(
        min_length=43,
        max_length=43,
        description="The browser's binding secret (43 base64url characters) whose sha256 was "
        "sent to `/auth/google/start` as `b`.",
    ),
]
FlowHandle = Annotated[
    SecretStr,
    Field(min_length=1, max_length=128, description="An opaque single-use handle."),
]


class GoogleCompleteRequest(StrictModel):
    handle: FlowHandle = Field(description="The `handle` of the `/auth/google?handle=` redirect.")
    binding: Binding
    device_tokens: Annotated[list[DeviceToken], Field(max_length=5)] | None = Field(
        default=None,
        description="ADR-0023: every trusted-browser token this browser holds (at most 5, no "
        "repeats; the account is not known yet). Only the signed-in account's own can match; "
        "it skips the Kuinn code step, never Google.",
    )

    @model_validator(mode="after")
    def _distinct_tokens(self) -> GoogleCompleteRequest:
        values = [t.get_secret_value() for t in self.device_tokens or []]
        if len(set(values)) != len(values):
            raise PydanticCustomError("mkos_invalid", "device_tokens must not repeat.")
        return self


class GoogleLinkRequest(StrictModel):
    link_handle: FlowHandle
    binding: Binding
    password: Annotated[SecretStr, Field(min_length=1, max_length=1024)]


class GoogleSignupRequest(StrictModel):
    """The sign-up form after Google: the email is Google's (verified, not editable), so only
    what Google cannot know is asked."""

    details_handle: FlowHandle
    binding: Binding
    brand_name: ProfileName
    owner_name: ProfileName
    phone: Phone | None = None
    website: Website | None = None


class GoogleConfigOut(WireModel):
    """Whether "Continue with Google" is offered: show the button only when `enabled`."""

    enabled: bool = Field(description="`GOOGLE_OAUTH_ENABLED`: Google sign-in is switched on.")


class GoogleDetailsOut(WireModel):
    """`needs_details`: no Kuinn account uses this Google email yet. Prefill the sign-up form."""

    email: Email = Field(description="Google's verified email: show it read-only.")
    email_verified: Literal[True] = Field(
        default=True, description='Always true: show "Verified by Google".'
    )
    owner_name: str | None = Field(default=None, description="Google's full name, editable.")
    given_name: str | None = None
    family_name: str | None = None
    suggested_brand_name: str | None = Field(
        default=None,
        description="From a company email's domain (never a mailbox provider's); editable.",
    )
    details_handle: NonBlankStr
    details_expires_in: Annotated[int, Field(ge=1, description="Seconds the handle stays valid.")]


class GoogleLinkOut(WireModel):
    """`link_required`: a Kuinn account already uses this email. Ask for its password once
    (`/auth/google/link`), then the usual code steps; Google is linked when they pass."""

    email: Email
    link_handle: NonBlankStr
    link_expires_in: Annotated[int, Field(ge=1, description="Seconds the handle stays valid.")]


class GoogleCompleteOut(WireModel):
    """Exactly one of `login`, `signup`, `details` and `link`, by `outcome`:

    - `sign_in`: `login` is a `LoginResult`: tokens, or the next step's challenge
      (`mfa_required`, `mfa_enrollment_required`, `password_change_required`) to continue with
      the usual `/auth/login/mfa*` and `/auth/password/expired` routes.
    - `pending`: `signup` is the account's latest sign-up request (pending, rejected or approved).
    - `needs_details`: `details` prefills the sign-up form; POST it to `/signup/google`.
    - `link_required`: `link` names the account; POST its password to `/auth/google/link`.
    """

    outcome: GoogleOutcome
    email: Email | None = Field(
        default=None,
        description="The Google account's verified email, on every outcome: show it on the "
        "steps that follow (the code step of `sign_in`, the password of `link_required`).",
    )
    login: LoginResult | None = None
    signup: SignupStatusOut | None = None
    details: GoogleDetailsOut | None = None
    link: GoogleLinkOut | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> GoogleCompleteOut:
        present = {
            "sign_in": self.login is not None,
            "pending": self.signup is not None,
            "needs_details": self.details is not None,
            "link_required": self.link is not None,
        }
        if not present[self.outcome] or sum(present.values()) != 1:
            raise ValueError("exactly the outcome's own field is set")
        return self


__all__ = [
    "GoogleCompleteOut",
    "GoogleCompleteRequest",
    "GoogleConfigOut",
    "GoogleDetailsOut",
    "GoogleLinkOut",
    "GoogleLinkRequest",
    "GoogleOutcome",
    "GoogleSignupRequest",
]
