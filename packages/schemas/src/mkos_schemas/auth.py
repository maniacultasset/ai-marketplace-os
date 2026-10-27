"""JWT claims issued by the backend (ADR-0006)."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from .base import NonBlankStr, StrictModel

Role = Literal["owner", "operator", "reviewer", "admin"]
# 2.0.0 (DPP-1, ADR-0021): how the session was proven. `pwd` password, `otp` a TOTP code,
# `rc` a recovery code (RFC 8176 names where one exists).
AuthMethod = Literal["pwd", "otp", "rc"]


class JWTClaims(StrictModel):
    sub: UUID
    tid: UUID
    roles: Annotated[list[Role], Field(min_length=1, max_length=4)]
    email: Annotated[str, Field(min_length=3, max_length=254)]
    typ: Literal["access", "refresh"]
    jti: Annotated[NonBlankStr, Field(max_length=128)]
    iss: Annotated[NonBlankStr, Field(max_length=256)]
    aud: Annotated[NonBlankStr, Field(max_length=256)]
    iat: Annotated[int, Field(ge=0)]
    exp: Annotated[int, Field(ge=0)]
    # 2.0.0, all optional: a token minted under MFA_ENFORCEMENT=off carries none of them.
    amr: Annotated[list[AuthMethod], Field(min_length=1, max_length=3)] | None = None
    mfa_at: Annotated[int, Field(ge=0)] | None = None  # when a code was last entered (unix s)
    sid: UUID | None = None  # the refresh-token family this session belongs to

    @field_validator("email")
    @classmethod
    def _email_shape(cls, value: str) -> str:
        if value.count("@") != 1:
            raise ValueError("email must contain exactly one @")
        return value

    @field_validator("roles")
    @classmethod
    def _unique_roles(cls, roles: list[Role]) -> list[Role]:
        if len(set(roles)) != len(roles):
            raise ValueError("duplicate roles")
        return roles

    @field_validator("amr")
    @classmethod
    def _unique_amr(cls, amr: list[AuthMethod] | None) -> list[AuthMethod] | None:
        if amr is not None and len(set(amr)) != len(amr):
            raise ValueError("duplicate amr values")
        return amr

    @model_validator(mode="after")
    def _exp_after_iat(self) -> JWTClaims:
        if self.exp <= self.iat:
            raise ValueError("exp must be after iat")
        if self.mfa_at is not None and self.mfa_at > self.iat:
            raise ValueError("mfa_at must not be after iat")
        return self
