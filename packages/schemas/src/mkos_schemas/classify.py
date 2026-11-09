"""AI seam payloads for one product classification (SI14; AI-30/31).

The backend sends the product's own text (the for-prompt facts, never a party or contact), up to 6
clean photos and the categories it can list (only those with a current template, lamps only while
`CATEGORY_LAMPS_ENABLED` is on). The pipeline answers with up to 3 candidates `{category,
confidence, rationale, evidence_refs}`; the confidence is calibrated from the agreement of its
samples, never the model's own number. The backend stores the result as a suggestion and never
writes `products.category` from it: the seller confirms, and a top confidence below 0.80 opens a
`category_review` task.

Field names and caps follow the pipeline's contract 1.1.0 (`mkos_pipeline.contracts`, whose JSON
Schemas are checked in under its `contracts/`), so those types can move here unchanged. Records:
the pipeline records the calls as agent `attribute`, trigger `vision` when photos were sent, else
`generate`, with prompt versions starting `classify/`.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, StringConstraints, field_validator

from .agent import AgentRunRecord
from .base import SCHEMA_VERSION, RequestId, Slug, WireModel
from .errors import ErrorReport
from .jobs import ContextImage

__all__ = [
    "CLASSIFY_CONFIDENCE_TASK_THRESHOLD",
    "MAX_CANDIDATES",
    "MAX_CATEGORY_OPTIONS",
    "MAX_CLASSIFY_IMAGES",
    "CategoryCandidate",
    "CategoryOption",
    "ClassifyRequest",
    "ClassifyResult",
    "DictionaryKey",
    "ProductText",
]

MAX_CLASSIFY_IMAGES: Final = 6
MAX_CATEGORY_OPTIONS: Final = 40
MAX_CANDIDATES: Final = 3
CLASSIFY_CONFIDENCE_TASK_THRESHOLD: Final = 0.80

_Text200 = Annotated[str, StringConstraints(max_length=200)]
_Text2000 = Annotated[str, StringConstraints(max_length=2000)]
_Confidence = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
DictionaryKey = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]


class CategoryOption(WireModel):
    """One category the backend offers (it has a current template)."""

    category: Slug
    name: _Text200 | None = None
    # The template's attribute keys, a hint of what the category holds.
    key_attributes: Annotated[list[DictionaryKey], Field(max_length=30)] = Field(
        default_factory=list
    )
    template_version: int | None = None


class ProductText(WireModel):
    """The product's own words: title, description, brand, the seller's category words and the
    for-prompt attribute facts as text (no party, no contact)."""

    title: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    description: _Text2000 | None = None
    brand: _Text200 | None = None
    seller_category: _Text200 | None = None
    attributes: Annotated[dict[DictionaryKey, _Text200], Field(max_length=40)] = Field(
        default_factory=dict
    )


class ClassifyRequest(WireModel):
    model_config = WireModel.model_config | ConfigDict(hide_input_in_errors=True)

    schema_version: str = SCHEMA_VERSION
    tenant_id: UUID
    product_id: UUID
    job_id: UUID | None = None
    request_id: RequestId
    text: ProductText
    images: Annotated[list[ContextImage], Field(max_length=MAX_CLASSIFY_IMAGES)] = Field(
        default_factory=list
    )
    categories: Annotated[
        list[CategoryOption], Field(min_length=1, max_length=MAX_CATEGORY_OPTIONS)
    ]

    @field_validator("categories")
    @classmethod
    def _unique_categories(cls, value: list[CategoryOption]) -> list[CategoryOption]:
        slugs = [option.category for option in value]
        if len(set(slugs)) != len(slugs):
            raise ValueError("categories must be unique")
        return value


class CategoryCandidate(WireModel):
    category: Slug
    confidence: _Confidence
    rationale: Annotated[str, StringConstraints(max_length=300)]
    # `text.title`, `text.description`, `text.brand`, `text.seller_category`, `attributes.<key>`
    # or `images[<image_id>]`.
    evidence_refs: Annotated[list[_Text200], Field(max_length=12)] = Field(default_factory=list)


class ClassifyResult(WireModel):
    status: Literal["ok", "failed"]
    candidates: Annotated[list[CategoryCandidate], Field(max_length=MAX_CANDIDATES)] = Field(
        default_factory=list
    )
    # True when the top candidate is below 0.80: the backend opens a seller task.
    needs_confirmation: bool = True
    calibration_version: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    samples: Annotated[int, Field(ge=0, le=3)] = 0
    agent_runs: Annotated[list[AgentRunRecord], Field(max_length=12)] = Field(default_factory=list)
    error: ErrorReport | None = None
