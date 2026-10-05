"""The brand profile read (owner step S16)."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from mkos_schemas.base import NonBlankStr, WireModel
from pydantic import Field

# The tenant slug is `ck_tenants_slug` (0001_core_tenancy; DATABASE.md tenants row): hyphens, no
# underscores, 2-40 chars. It is not the category/rule `Slug` (`^[a-z0-9_]{2,64}$`).
TenantSlug = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{1,39}$")]


class TenantOut(WireModel):
    id: UUID
    name: NonBlankStr
    slug: TenantSlug
    tone: str | None = None
    guidelines: list[str] = Field(default_factory=list)
    glossary: dict[str, str] = Field(default_factory=dict)
    status: Literal["trial", "active", "suspended", "offboarded"]
