"""The brand profile read (owner step S16)."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from mkos_schemas.base import NonBlankStr, Slug, WireModel
from pydantic import Field


class TenantOut(WireModel):
    id: UUID
    name: NonBlankStr
    slug: Slug
    tone: str | None = None
    guidelines: list[str] = Field(default_factory=list)
    glossary: dict[str, str] = Field(default_factory=dict)
    status: Literal["trial", "active", "suspended", "offboarded"]
