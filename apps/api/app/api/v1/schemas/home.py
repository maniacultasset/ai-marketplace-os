"""The Home summary (client feedback 11 Nov 2026, item 6): one read for the dashboard.

Every number is an exact count over the tenant's listing, task and issue rows (no page walk, no
cap), and each bucket has one definition, written in `app.services.home` and in the field
descriptions below, so the Home tiles reconcile with the Products list and the Inbox.

`listings` counts every listing that is not archived exactly once: `live + partly_live +
in_progress + problem + ready_to_approve + ready_to_publish + draft + inactive == total`.
`needs_you` is the caller's own work, so it depends on the caller's role: listings to approve
count only for a role that approves, listings to publish only for a role that publishes and only
while publishing to that marketplace is on."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from mkos_schemas.base import WireModel
from mkos_schemas.enums import Marketplace
from pydantic import Field

Count = Annotated[int, Field(ge=0)]
NeedsYouKind = Literal["blocked", "question", "approve", "publish"]
NeedsYouAction = Literal["fix", "answer", "approve", "publish"]
ActivityState = Literal["completed", "failed", "pending"]
ActivityKind = Literal["write", "send", "photo_check", "import", "brand", "fix"]
AccountState = Literal["connected", "needs_reconnect", "not_connected"]
NextStep = Literal["connect", "reconnect", "add_listing", "fix", "none"]


class ListingCounts(WireModel):
    """Each listing that is not archived, in exactly one bucket (module docstring)."""

    total: Count
    live: Count = Field(description="The marketplace confirmed the listing live.")
    partly_live: Count = Field(description="The marketplace has some of its variants live.")
    live_products: Count = Field(
        description="Distinct products with at least one live or partly live listing."
    )
    in_progress: Count = Field(
        description=(
            "Kuinn or the marketplace is working: writing, sending, or sent and waiting for the "
            "marketplace to confirm. Nothing to do."
        )
    )
    problem: Count = Field(
        description=(
            "Stopped: the marketplace rejected it, the last step failed, a check blocked it, or "
            "an open blocking issue sits on its current version."
        )
    )
    ready_to_approve: Count = Field(
        description="Written and checked, with no open blocking issue: a person approves next."
    )
    ready_to_publish: Count = Field(description="Approved: a person publishes next.")
    draft: Count = Field(description="Created, not written yet.")
    inactive: Count = Field(description="Published once, now inactive or removed.")


class MarketplaceCounts(WireModel):
    marketplace: Marketplace
    listings: Count
    live: Count
    partly_live: Count
    in_progress: Count
    problem: Count
    ready_to_approve: Count
    ready_to_publish: Count


class NeedsYouCounts(WireModel):
    """The caller's work. `total = blocked + questions + ready_to_approve + ready_to_publish`."""

    total: Count
    blocked: Count = Field(
        description=(
            "Problem listings that wait on a fix rather than an answer: a listing whose every "
            "open blocking issue waits on an open question is counted under `questions`."
        )
    )
    questions: Count = Field(
        description="Open questions an open blocking issue waits on (`TaskOut.blocking`)."
    )
    ready_to_approve: Count = Field(
        description="Listings ready to approve; 0 for a role that cannot approve."
    )
    ready_to_publish: Count = Field(
        description=(
            "Approved listings the caller can publish now; 0 for a role that cannot publish. "
            "An approved listing on a marketplace whose publishing is paused is not counted."
        )
    )
    publishing_paused: Count = Field(
        description=(
            "Approved listings waiting while publishing to their marketplace is paused, for a "
            "role that approves or publishes. Said, never counted in `total`."
        )
    )
    improve: Count = Field(
        description="Open questions that hold no listing back (answering improves a listing)."
    )


class NeedsYouItem(WireModel):
    """One thing to do: the product, the issue in plain words and the action."""

    kind: NeedsYouKind
    action: NeedsYouAction
    product_id: UUID | None = None
    product_sku: str | None = None
    product_title: str | None = None
    marketplace: Marketplace | None = None
    listing_id: UUID | None = None
    task_id: UUID | None = None
    issue_id: UUID | None = None
    issue: str = Field(max_length=500, description="What is wrong, in the seller's words.")
    issue_count: Count = Field(
        default=0, description="Open blocking issues on the listing (`blocked` only)."
    )
    at: datetime


class ActivityItem(WireModel):
    """One recent agent action, newest first."""

    kind: ActivityKind
    state: ActivityState
    text: str = Field(max_length=300, description="What happened, in plain words.")
    product_id: UUID | None = None
    product_title: str | None = None
    listing_id: UUID | None = None
    marketplace: Marketplace | None = None
    at: datetime


class MarketplaceStatus(WireModel):
    marketplace: Marketplace
    enabled: bool = Field(description="Listing on this marketplace is open on this deployment.")
    account: AccountState
    publishing_enabled: bool
    next_step: NextStep = Field(
        description=(
            "`connect` (no account), `reconnect` (access expired), `fix` (a listing here is "
            "stopped), `add_listing` (connected, nothing listed yet), else `none`."
        )
    )


class HomeSummaryOut(WireModel):
    as_of: datetime
    listings: ListingCounts
    by_marketplace: Annotated[list[MarketplaceCounts], Field(max_length=2)]
    needs_you: NeedsYouCounts
    needs_you_items: Annotated[list[NeedsYouItem], Field(max_length=5)]
    activity: Annotated[list[ActivityItem], Field(max_length=8)]
    marketplaces: Annotated[list[MarketplaceStatus], Field(max_length=2)]
    products: Count = Field(description="Products that are not deleted (styles and standalones).")
