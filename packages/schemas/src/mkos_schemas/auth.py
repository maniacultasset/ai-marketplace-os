"""JWT claims issued by the backend (ADR-0006)."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from .base import NonBlankStr, StrictModel

Role = Literal["owner", "operator", "reviewer", "admin"]


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

    @model_validator(mode="after")
    def _exp_after_iat(self) -> JWTClaims:
        if self.exp <= self.iat:
            raise ValueError("exp must be after iat")
        return self
