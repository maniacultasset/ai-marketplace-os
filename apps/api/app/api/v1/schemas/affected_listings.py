"""The listings an answer reaches and whether each can be redrafted now (G1): returned by
`POST /inbox/groups/answer` and, for a brand party default, by `PATCH /brand-profile`
(`app.services.inbox_groups.listing_verdicts`)."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from mkos_schemas.base import WireModel
from mkos_schemas.enums import Marketplace
from pydantic import Field, StringConstraints

from app.services.sections import Section

__all__ = ["AffectedListingOut", "RedraftBlock"]

ListingStatus = Annotated[str, StringConstraints(max_length=40)]
RedraftBlock = Literal[
    "busy", "paused", "other_questions_open", "nothing_to_redraft", "not_written"
]


class AffectedListingOut(WireModel):
    listing_id: UUID
    product_id: UUID
    marketplace: Marketplace
    status: ListingStatus
    redraft: bool = Field(
        description="It can be written again now with `POST /listings/{listing_id}/regenerate` "
        "and these `sections` with `for_answers: true`: only when nothing blocks it and "
        "`sections` is not empty (`redraft_blocked` says why not)."
    )
    redraft_blocked: RedraftBlock | None = Field(
        default=None,
        description="Why not now: `not_written` (never generated: generate it instead), "
        "`nothing_to_redraft` (the answered fact feeds no unlocked section), `busy` (being "
        "written or published now), `paused`, or `other_questions_open` (other seller "
        "questions, `open_questions` of them, still stand on the listing: redraft once every "
        "answer is in). Null when `redraft` is true.",
    )
    open_questions: int | None = Field(
        default=None,
        ge=1,
        description="`other_questions_open` only: how many other live seller questions stand "
        "on the listing; null otherwise.",
    )
    sections: list[Section] = Field(
        default_factory=list,
        max_length=5,
        description="The sections the answered fact feeds, in card order, less the ones every "
        "path of which the seller locked; empty when the fact reaches no section.",
    )
