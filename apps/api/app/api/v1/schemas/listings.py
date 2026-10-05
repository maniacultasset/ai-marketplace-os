"""Listing read, edit, validate and regenerate models (owner steps S18, S19)."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from mkos_schemas.base import AttrKey, Slug, StrictModel, WireModel
from mkos_schemas.enums import ListingStatus, Marketplace, MarketplaceState
from mkos_schemas.listing import AttributeValue, CanonicalListing, ListingContent, ListingKeywords
from mkos_schemas.validation import ValidationSummary
from pydantic import AwareDatetime, Field

from app.api.pagination import Page
from app.api.v1.schemas.batches import Instructions


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


class RegenerateRequest(StrictModel):
    instructions: Instructions | None = None


class DryRunRequest(StrictModel):
    listing: CanonicalListing
