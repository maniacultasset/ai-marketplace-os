"""Image candidate request and response models (SI7, ADR-0030 proposed).

A candidate is a proposed product image: a white-background edit of the main photo (`white_background`,
one model call), a deterministic re-frame of it (`fix`), or a render of the size chart
(`size_chart`). It becomes a product image only when the seller accepts it.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Final, Literal
from uuid import UUID

from mkos_schemas.base import NonBlankStr, StrictModel, WireModel
from pydantic import AwareDatetime, Field, ValidationInfo, field_serializer, field_validator
from pydantic_core import PydanticCustomError

CandidateKind = Literal["white_background", "fix", "size_chart"]
CandidateStatus = Literal["queued", "ready", "failed", "accepted", "rejected"]
FailureReason = Literal[
    "fidelity",
    "not_white",
    "too_small",
    "source_unusable",
    "no_size_chart",
    "model_failed",
    "credit_used_up",
    "budget",
    "source_changed",
]
MAX_CANDIDATES_LISTED: Final = 50
_PHOTO_KINDS: Final = frozenset({"white_background", "fix"})


class ImageCandidateIn(StrictModel):
    kind: CandidateKind
    source_image_id: UUID | None = Field(
        default=None,
        validate_default=True,
        description="The product's live main image; required for `white_background` and `fix`, "
        "refused for `size_chart`.",
    )

    @field_validator("source_image_id")
    @classmethod
    def _source_for_kind(cls, value: UUID | None, info: ValidationInfo) -> UUID | None:
        kind = info.data.get("kind")
        if kind is None:
            return value
        if kind in _PHOTO_KINDS and value is None:
            raise PydanticCustomError("mkos_invalid", "A photo candidate needs `source_image_id`.")
        if kind == "size_chart" and value is not None:
            raise PydanticCustomError(
                "mkos_invalid", "A size-chart candidate takes no `source_image_id`."
            )
        return value


class ImageCandidateAccepted(WireModel):
    candidate_id: UUID
    status: CandidateStatus
    job_id: UUID | None = Field(default=None, description="Null when no new job was needed.")
    poll_url: NonBlankStr | None = None
    accepted_image_id: UUID | None = Field(
        default=None,
        description="Set only with status `accepted`: this source was already replaced by "
        "the candidate's image, which is still the live main of the same product and "
        "colourway. No new job was started.",
    )


class FidelityOut(WireModel):
    mask_iou: float | None = None
    colour_distance: float
    agreement: float
    hole_share: float


class ImageCandidateOut(WireModel):
    id: UUID
    kind: CandidateKind
    source_image_id: UUID | None = None
    colourway_id: UUID | None = None
    status: CandidateStatus
    failure_reason: FailureReason | None = None
    width: int | None = None
    height: int | None = None
    fidelity: FidelityOut | None = None
    model: str | None = None
    prompt_version: str | None = None
    cost_usd: Annotated[Decimal, Field(max_digits=12, decimal_places=6, ge=0)] = Decimal("0")
    preview_url: Annotated[str | None, Field(max_length=4096)] = Field(
        default=None,
        description="A short-lived signed GET of the candidate; null on the reject answer.",
    )
    source_preview_url: Annotated[str | None, Field(max_length=4096)] = Field(
        default=None,
        description="A short-lived signed GET of the source photo; null on the reject answer.",
    )
    accepted_image_id: UUID | None = None
    created_at: AwareDatetime

    @field_serializer("cost_usd", when_used="json")
    def _serialize_cost_usd(self, value: Decimal) -> str:
        return format(value.quantize(Decimal("0.000001")), "f")


class ImageCandidatePage(WireModel):
    items: Annotated[list[ImageCandidateOut], Field(max_length=MAX_CANDIDATES_LISTED)]
