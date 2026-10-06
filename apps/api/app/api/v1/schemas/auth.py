"""Auth and members request and response models (owner step S10)."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from mkos_schemas.auth import Role
from mkos_schemas.base import NonBlankStr, StrictModel, WireModel
from pydantic import Field, SecretStr, StrictBool, model_validator
from pydantic_core import PydanticCustomError

from app.api.pagination import Page

Email = Annotated[str, Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+$")]
RefreshToken = Annotated[SecretStr, Field(min_length=1, max_length=4096)]


class LoginRequest(StrictModel):
    email: Email
    password: Annotated[SecretStr, Field(min_length=1, max_length=1024)]


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


class MeOut(WireModel):
    user_id: UUID
    email: Email
    display_name: str | None = None
    tenant_id: UUID
    role: Role
    memberships: list[MembershipOut]


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
