"""The grouped Inbox (G1): identical open questions and issues across the brand's products as one
group, and one answer for many products (`app.services.inbox_groups`)."""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from mkos_schemas.base import StrictModel, WireModel
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import Marketplace
from pydantic import AwareDatetime, Field, StringConstraints, field_validator
from pydantic_core import PydanticCustomError

from app.api.v1.schemas.issues import IssueOut
from app.api.v1.schemas.products import AnswerScalar
from app.api.v1.schemas.tasks import TaskKind, TaskOut
from app.services.sections import Section

GroupScope = Literal["brand", "product_fact", "exemption", "per_item"]
SkipReason = Literal["has_value", "closed", "not_allowed", "wrong_level", "gone"]
ListingStatus = Annotated[str, StringConstraints(max_length=40)]
RedraftBlock = Literal["busy", "paused", "other_questions_open"]


class InboxGroupMemberOut(WireModel):
    """One item of a group: a question (`task_id`) or an issue (`issue_id`), its product, and
    whether the product already holds the fact (`has_value`: an answer for many keeps it)."""

    task_id: UUID | None = None
    issue_id: UUID | None = None
    product_id: UUID | None = None
    sku: str | None = None
    title: str | None = None
    listing_ids: list[UUID] = Field(
        default_factory=list,
        max_length=4,
        description="The listings this item stands on: an issue's own listing, or the "
        "product's listings that are not archived (a question is shared by both marketplaces).",
    )
    has_value: bool = Field(
        default=False,
        description="The product (or its style) already holds this fact, or for a barcode "
        "question already has a barcode or an exemption: an answer for many keeps it as it is.",
    )
    at: AwareDatetime


class InboxGroupOut(WireModel):
    key: Annotated[str, StringConstraints(max_length=200)] = Field(
        description="Opaque. Send it back as `group` on `POST /inbox/groups/answer`."
    )
    item_kind: Literal["task", "issue"]
    scope: GroupScope = Field(
        description="What one answer may do: `brand` (set the brand manufacturer, marketer, "
        "packer or importer once with `PATCH /brand-profile`), `product_fact` (one value for "
        "every product without one), "
        "`exemption` (mark the products without a barcode as GTIN exempt), `per_item` (each "
        "item needs its own fix)."
    )
    count: int = Field(ge=1, description="The items in the group (every one, past the cap too).")
    products: int = Field(ge=0)
    listings: int = Field(ge=0, description="The distinct listings the items stand on.")
    blocking: bool = Field(description="At least one item holds a listing back (an ERROR).")
    at: AwareDatetime = Field(description="When the newest item opened.")
    field: str | None = Field(
        default=None,
        description="The dictionary key a question asks about (`gender`, `manufacturer`, "
        "`identifier`), or the field family (`images[]`, `content.bullets[]`).",
    )
    task_kind: TaskKind | None = None
    error_code: ErrorCode | None = None
    marketplace: Marketplace | None = Field(default=None, description="Issues only.")
    answer: dict[str, Any] | None = Field(
        default=None,
        description="The AnswerSpec every question of the group shares (the same allowed "
        "values); null for an issue group.",
    )
    categories: list[str] = Field(default_factory=list, max_length=20)
    brand_value_set: bool = Field(
        default=False,
        description="`brand` only: the brand's default for this party (manufacturer, marketer, "
        "packer or importer) is set already, so the items were written before it was; check "
        "or redraft them to use it.",
    )
    sample_task: TaskOut | None = Field(
        default=None, description="The newest question, as `GET /tasks` lists it."
    )
    sample_issue: IssueOut | None = Field(
        default=None, description="The newest issue, as `GET /issues` lists it."
    )
    members: list[InboxGroupMemberOut] = Field(default_factory=list, max_length=1000)
    members_more: bool = False


class InboxGroupsOut(WireModel):
    enabled: bool = Field(
        description="False while the grouped Inbox is switched off: `groups` is empty, use the "
        "plain lists."
    )
    as_of: AwareDatetime
    view: Literal["all", "questions", "issues"]
    total_items: int = Field(
        ge=0,
        description="Every item of the view, past the group cap too. It equals the summary's "
        "`inbox.open_items` (all), `inbox.questions` or `inbox.issues`.",
    )
    groups: list[InboxGroupOut] = Field(default_factory=list, max_length=300)
    more: bool = Field(default=False, description="More groups than the 300 listed.")


class InboxGroupAnswerIn(StrictModel):
    """One answer for up to 100 questions of one group. Products that already hold a value are
    skipped, never overwritten."""

    group: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    task_ids: list[UUID] = Field(min_length=1, max_length=100)
    value: AnswerScalar

    @field_validator("task_ids")
    @classmethod
    def _unique(cls, ids: list[UUID]) -> list[UUID]:
        if len(set(ids)) != len(ids):
            raise PydanticCustomError("mkos_invalid", "Send each question once.")
        return ids


class AppliedOut(WireModel):
    task_id: UUID
    product_id: UUID


class SkippedOut(WireModel):
    task_id: UUID
    product_id: UUID | None = None
    reason: SkipReason = Field(
        description="`has_value`: the product already holds a value (kept); `closed`: the "
        "question is no longer open; `not_allowed`: the value is not one this product's "
        "category accepts; `wrong_level`: the fact belongs on another level of the product "
        "(its style or its sizes); `gone`: no such question in this brand."
    )


class AffectedListingOut(WireModel):
    listing_id: UUID
    product_id: UUID
    marketplace: Marketplace
    status: ListingStatus
    redraft: bool = Field(
        description="It can be written again now with `POST /listings/{listing_id}/regenerate` "
        "and these `sections` with `for_answers: true` (`redraft_blocked` says why not)."
    )
    redraft_blocked: RedraftBlock | None = Field(
        default=None,
        description="Why not now: `busy` (never written, or being written), `paused`, or "
        "`other_questions_open` (another seller question still stands on the listing: redraft "
        "once every answer is in). Null when `redraft` is true.",
    )
    sections: list[Section] = Field(
        default_factory=list,
        max_length=5,
        description="The sections the answered fact feeds, in card order, less the ones every "
        "path of which the seller locked; empty when the fact reaches no section.",
    )


class InboxGroupAnswerOut(WireModel):
    applied: list[AppliedOut]
    skipped: list[SkippedOut]
    listings: list[AffectedListingOut] = Field(
        description="The listings (not archived) of the products answered. A listing uses the "
        "answer only once it is written again."
    )
