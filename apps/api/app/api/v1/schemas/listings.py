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
from app.api.v1.schemas.resolution import RemoteCheckOut
from app.core.text import CONTROL_CHAR_MESSAGE, has_control_char
from app.services.listing_display import DisplayState


class ListingOut(WireModel):
    id: UUID
    product_id: UUID
    marketplace: Marketplace
    status: ListingStatus
    status_reason: str | None = None
    display_state: DisplayState = Field(
        default="draft",
        description=(
            "The state every screen shows, computed by the server. `needs_you`: open blocking "
            "issues on the current version (`needs_you_count` of them) keep a person from "
            "approving it, whatever `status` says. The other values follow `status` (validated "
            "is ready_to_approve, approved is ready_to_list, publishing is submitting, failed is "
            "error). A stale reviewable listing is checked again with today's rules on read. "
            "Always sent; an older payload without it reads as its status's value."
        ),
    )
    needs_you_count: int = Field(
        default=0,
        ge=0,
        description="Open blocking issues on the current version (the seller's fixes).",
    )
    marketplace_state: MarketplaceState
    category: Slug | None = None
    current_version_id: UUID | None = None
    approved_version_id: UUID | None = None
    live_url: str | None = None
    external_id: str | None = Field(
        default=None,
        max_length=128,
        description=(
            "The marketplace's id for the product (the Amazon ASIN), set once the marketplace "
            "reads it. With status published and marketplace_state inactive the product is on "
            "the marketplace but not buyable yet."
        ),
    )
    remote_check: RemoteCheckOut | None = Field(
        default=None,
        description=(
            "SI11: the marketplace's dry-run check of a version before approval (Amazon "
            "VALIDATION_PREVIEW): pending, clean, issues or failed. Null when none ran."
        ),
    )
    status_changed_at: AwareDatetime
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @model_validator(mode="before")
    @classmethod
    def _display_from_status(cls, data: Any) -> Any:
        # an older payload (or example) without the display state reads as its status's value
        if isinstance(data, dict) and "display_state" not in data and "status" in data:
            from app.services.listing_display import display_state

            class _Row:
                status = str(data["status"])

            return {**data, "display_state": display_state(_Row, 0)}
        return data


class ListingDetail(ListingOut):
    content: CanonicalListing | None = None
    validation: ValidationSummary | None = None
    can_approve: bool | None = Field(
        default=None,
        description=(
            "Whether your role and maker-checker let you approve the current version (SI10). "
            "Null unless the listing is reviewable with a current version. The approve call "
            "still checks the rest (permission, stale checks, images)."
        ),
    )
    approve_hint: (
        Literal["own_work", "no_other_approver", "not_approver", "owner_can_enable"] | None
    ) = Field(
        default=None,
        description=(
            "Why `can_approve` is false: `own_work` (you made or edited this version), "
            "`no_other_approver` (and nobody else in the brand can approve it yet), "
            "`owner_can_enable` (the same, and you are an owner: turning on the brand's "
            "`owner_self_approval` lets you approve your own work, ADR-0031) or `not_approver` "
            "(your role does not approve)."
        ),
    )
    self_approval: bool | None = Field(
        default=None,
        description=(
            "ADR-0031: true when `can_approve` is true and you made or edited this version (an "
            "owner under the brand's `owner_self_approval`): the approval is recorded as a "
            "self-approval. Null when `can_approve` is null."
        ),
    )


class ListingPage(Page[ListingOut]):
    pass


class AuditReviewOut(WireModel):
    """The decision behind an approve or reject event (prod QA 2026-11-09, the Activity tab)."""

    listing_version_id: UUID | None = None
    version: int | None = Field(default=None, ge=1, description="The version's number.")
    reason_code: str | None = Field(
        default=None,
        description="The reject reason. The reviewer's free-text feedback is never sent here.",
    )
    self_approval: bool = Field(
        default=False,
        description=(
            "ADR-0031: an owner approved its own work under the brand's `owner_self_approval`."
        ),
    )


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
    actor_name: str | None = Field(
        default=None,
        description=(
            "Item 17: for a person (`actor_type` `user`) who is a member of this brand, the "
            "display name, else the email. Null for Kuinn, the support team, an erased account "
            "and anyone who is not a member."
        ),
    )
    review: AuditReviewOut | None = Field(
        default=None,
        description=(
            "For `listing.approved` and `listing.rejected`: the version decided on, the "
            "reason code (never the free-text feedback) and whether the approval was a "
            "self-approval. Null for other events (a `listing.self_approved` event, ADR-0031, "
            "sits beside its `listing.approved`)."
        ),
    )


class ListingHistoryPage(Page[AuditEventOut]):
    pass


class VersionPersonOut(WireModel):
    """A member of this brand named on a version (item 17, client feedback 11 Nov 2026)."""

    user_id: UUID
    name: str | None = Field(
        default=None,
        description="The display name, else the email. Null for an erased account.",
    )


class ListingVersionOut(WireModel):
    """Who made, edited and approved one version of a listing (item 17). The content itself is
    read through `listings_get`."""

    id: UUID
    version: int = Field(ge=1, description="The version's number.")
    origin: Literal["generation", "regeneration", "edit", "import"]
    written_by: Literal["kuinn", "person"] = Field(
        description=(
            "Who wrote the content: `kuinn` for a generation or regeneration (an automatic fix "
            "included), `person` for an edit or an import."
        )
    )
    created_at: AwareDatetime
    generated_by: VersionPersonOut = Field(
        description=(
            "The maker: the member who asked Kuinn for the draft (or imported it); an edit "
            "keeps its parent's."
        )
    )
    edited_by: VersionPersonOut | None = Field(
        default=None, description="The member whose edit made this version."
    )
    approved_by: VersionPersonOut | None = None
    approved_at: AwareDatetime | None = None
    self_approved: bool = Field(
        default=False,
        description="ADR-0031: an owner approved its own work under `owner_self_approval`.",
    )


class ListingVersionPage(Page[ListingVersionOut]):
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

    @model_validator(mode="before")
    @classmethod
    def _no_unknown_nested_keys(cls, data: Any) -> Any:
        # Invariant 4: `ListingContent`/`ListingKeywords` are shared WireModels that drop unknown
        # keys; on a human edit a marketplace-only or unknown key is a 422, never a silent drop.
        if isinstance(data, dict):
            for name, model in (("content", ListingContent), ("keywords", ListingKeywords)):
                value = data.get(name)
                if isinstance(value, dict) and not set(value) <= set(model.model_fields):
                    raise PydanticCustomError(
                        "mkos_invalid", "Unknown key in {name}.", {"name": name}
                    )
        return data

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
