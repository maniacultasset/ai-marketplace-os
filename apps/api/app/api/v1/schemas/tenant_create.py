"""An owner adds another brand (ADR-0032): the brand name and an optional website, as on the
sign-up form. The request is strict (`extra="forbid"`), so a body naming a tenant, a role, a
status or a trial is 422."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from mkos_schemas.base import StrictModel, WireModel
from pydantic import AwareDatetime, Field

from app.api.v1.schemas.signup import Website
from app.api.v1.schemas.tenants import ProfileName, TenantSlug


class TenantCreate(StrictModel):
    name: ProfileName
    website: Website | None = None


class TenantCreated(WireModel):
    """The new brand. The caller is its owner; switch to it with `POST /auth/switch-tenant`."""

    id: UUID
    name: str
    slug: TenantSlug
    status: Literal["trial", "active"]
    trial_ends_at: AwareDatetime | None = Field(
        default=None, description="When the new brand's trial ends (the sign-up's trial rule)."
    )
    role: Literal["owner"] = "owner"
