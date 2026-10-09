"""Listing read, edit, validate and regenerate models (owner steps S18, S19)."""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from mkos_schemas.base import AttrKey, Slug, StrictModel, WireModel
from mkos_schemas.enums import ListingStatus, Marketplace, MarketplaceState
from mkos_schemas.listing import AttributeValue, CanonicalListing, ListingContent, ListingKeywords
from mkos_schemas.validation import ValidationSummary
from pydantic import AwareDatetime, Field, model_validator
from pydantic_core import PydanticCustomError

from app.api.pagination import Page
from app.api.v1.schemas.batches import Instructions
from app.core.text import CONTROL_CHAR_MESSAGE, has_control_char


class ListingOut(WireModel):
    id: UUID
    product_id: UUID
    marketplace: Marketplace
    status: ListingStatus
    status_reason: str | None = None
    marketplace_state: MarketplaceState
    category: Slug | None = None
    current_version_id: UUID | None = None
    approved_version_id: UUID | None = None
    live_url: str | None = None
    status_changed_at: AwareDatetime
    created_at: AwareDatetime
    updated_at: AwareDatetime


class ListingDetail(ListingOut):
    content: CanonicalListing | None = None
    validation: ValidationSummary | None = None


class ListingPage(Page[ListingOut]):
    pass


class AuditEventOut(WireModel):
    """One `audit_log` row of a listing. No `actor_email` and no `ip`: the history is a seller
    read, and both are personal data (DATABASE.md §5.2)."""

    id: UUID
    action: str
    actor_type: Literal["user", "system", "staff"]
    actor_user_id: UUID | None = None
    entity_type: str
    entity_id: UUID
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    reason: str | None = None
    request_id: str | None = None
    job_id: UUID | None = None
    occurred_at: AwareDatetime


class ListingHistoryPage(Page[AuditEventOut]):
    pass


class ListingStatusOut(WireModel):
    listing_id: UUID
    status: ListingStatus
    status_reason: str | None = None
    marketplace_state: MarketplaceState
    status_changed_at: AwareDatetime


class ValidateRequest(StrictModel):
    listing_id: UUID
    listing_version_id: UUID | None = None


class ListingContentPatch(StrictModel):
    """A human edit: it creates a new version, and the editor can no longer approve it."""

    content: ListingContent
    keywords: ListingKeywords | None = None
    attributes: Annotated[dict[AttrKey, AttributeValue] | None, Field(max_length=200)] = None

    @model_validator(mode="after")
    def _no_control_chars(self) -> ListingContentPatch:
        # NUL breaks jsonb (a 500); other C0 controls are refused as well (\n and \t pass).
        if has_control_char(self.model_dump(mode="python")):
            raise PydanticCustomError("mkos_invalid", CONTROL_CHAR_MESSAGE)
        return self


class RegenerateRequest(StrictModel):
    instructions: Instructions | None = None


class DryRunRequest(StrictModel):
    listing: CanonicalListing
