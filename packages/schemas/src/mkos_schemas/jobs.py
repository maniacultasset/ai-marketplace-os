"""Job envelopes and AI seam payloads."""

from __future__ import annotations

import binascii
import ipaddress
from base64 import b64decode
from typing import Annotated, Any, Final, Literal
from urllib.parse import urlparse
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field, SecretStr, field_validator, model_validator

from .agent import AgentRunRecord
from .base import (
    SCHEMA_VERSION,
    AttrKey,
    NonBlankStr,
    RequestId,
    Slug,
    StrictModel,
    WireModel,
    walk_seam_dicts_and_text,
)
from .enums import ImageSlot, JobKind, JobState, Locale, Marketplace, PipelineStage
from .errors import ErrorReport
from .images import ImageReport
from .listing import AttributeValue, CanonicalListing
from .paths import is_valid_field_path
from .rules import Condition, RuleSet
from .validation import ValidationSummary

BrandChunkType = Literal["tone_rule", "glossary", "compliance", "example"]
TerminalState = Literal["generated", "needs_review", "blocked", "failed"]


class GenerateListingRequest(StrictModel):
    product_id: UUID
    marketplace: Marketplace
    category: Slug | None = None
    user_instructions: Annotated[NonBlankStr | None, Field(default=None, max_length=2000)] = None
    force_regenerate: bool = False


class JobAccepted(WireModel):
    job_id: UUID
    listing_id: UUID | None = None
    status: JobState = JobState.QUEUED
    estimated_duration_seconds: Annotated[int, Field(ge=0)] = 30
    poll_url: NonBlankStr


class JobEnvelope(WireModel):
    schema_version: str = SCHEMA_VERSION
    job_id: UUID
    kind: JobKind
    status: JobState
    stage: PipelineStage | None = None
    progress_pct: Annotated[int, Field(ge=0, le=100)] = 0
    attempt: Annotated[int, Field(ge=0, le=3)] = 0
    max_attempts: Annotated[int, Field(ge=1, le=3)] = 3
    listing_id: UUID | None = None
    result_url: str | None = None
    error: ErrorReport | None = None
    request_id: RequestId
    queued_at: AwareDatetime
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None


class BrandChunkDTO(WireModel):
    id: UUID
    tenant_id: UUID
    type: BrandChunkType
    text: Annotated[NonBlankStr, Field(max_length=2000)]
    similarity: Annotated[float, Field(ge=-1.0, le=1.0)]
    source_type: Literal["brand_document", "brand_profile", "listing_version"]
    source_id: UUID
    chunk_index: Annotated[int, Field(ge=0)] = 0

    @model_validator(mode="after")
    def _walk_strings(self) -> BrandChunkDTO:
        walk_seam_dicts_and_text(self)
        return self


class AttributeSpec(WireModel):
    requirement: Literal["required", "conditional", "optional", "unsupported"]
    marketplace_field: str | None = None
    value_type: Literal["text", "enum", "number", "boolean", "measurement"]
    allowed_values: list[str] | None = None
    value_map: dict[str, str] = Field(default_factory=dict)
    max_length: int | None = Field(default=None, gt=0)
    unit: str | None = None
    when: Annotated[list[Condition], Field(max_length=4)] = Field(default_factory=list)


class CategoryTemplate(WireModel):
    marketplace: Marketplace
    category: Slug
    external_category_id: str | None = None
    required_attributes: list[AttrKey] = Field(default_factory=list)
    optional_attributes: list[AttrKey] = Field(default_factory=list)
    conditional_attributes: list[AttrKey] = Field(default_factory=list)
    unsupported_attributes: list[AttrKey] = Field(default_factory=list)
    specs: dict[AttrKey, AttributeSpec] = Field(default_factory=dict)
    variation_themes: list[list[AttrKey]] = Field(default_factory=list)

    def for_generation(self) -> CategoryTemplate:
        cleaned_specs: dict[str, AttributeSpec] = {}
        for key, spec in self.specs.items():
            cleaned_specs[key] = spec.model_copy(
                update={"marketplace_field": None, "value_map": {}},
                deep=True,
            )
        return self.model_copy(update={"specs": cleaned_specs}, deep=True)

    @model_validator(mode="after")
    def _walk(self) -> CategoryTemplate:
        walk_seam_dicts_and_text(self)
        return self


class ModelCredentials(WireModel):
    """Seam type (CANON.md:122, WireModel): unknown keys from a newer backend are dropped."""

    model_config = WireModel.model_config | ConfigDict(hide_input_in_errors=True)

    provider: Literal["openai", "deepseek"]
    api_key: SecretStr
    api_base: str | None = Field(default=None, max_length=2048)
    model: Annotated[NonBlankStr, Field(max_length=128)]
    timeout_seconds: Annotated[float, Field(gt=0, le=600)] = 60.0

    @field_validator("api_key", mode="before")
    @classmethod
    def _key_non_blank(cls, value: object) -> object:
        if isinstance(value, str) and value.strip() == "":
            raise ValueError("api_key required")
        return value

    @field_validator("api_base")
    @classmethod
    def _https_only(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlparse(value)
        if parsed.scheme != "https":
            raise ValueError("api_base must be https")
        if parsed.username or parsed.password:
            raise ValueError("userinfo forbidden")
        host = parsed.hostname
        if not host:
            raise ValueError("host required")
        if host in {"localhost", "127.0.0.1", "169.254.169.254"}:
            raise ValueError("private host forbidden")
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            return value
        if addr.is_private or addr.is_link_local or addr.is_loopback:
            raise ValueError("private host forbidden")
        return value


class Provenance(WireModel):
    """Where one product value came from (1.2.0). Absent: treat the value as seller-confirmed."""

    source: str
    confidence: Annotated[float | None, Field(ge=0.0, le=1.0)]
    confirmed: bool


class ObservedValue(WireModel):
    """One value a source gave for a fact the conflict scan found disagreeing (1.2.0)."""

    source: str
    value: AttributeValue
    evidence_ref: str


class FactConflict(WireModel):
    """Sources that disagree on one product key (1.2.0); the pipeline phrases a question."""

    key: str
    values: Annotated[list[ObservedValue], Field(min_length=2)]


# 1.2.0 (plan C11): up to 6 product images plus the packaging label, each a JPEG the backend has
# already scaled to at most 1024 px. Lowering either cap later is a MAJOR change.
MAX_CONTEXT_IMAGES: Final = 7
MAX_CONTEXT_IMAGE_PX: Final = 1024
MAX_CONTEXT_IMAGE_B64_CHARS: Final = 2_097_152  # about 1.5 MiB of JPEG
_JPEG_MAGIC: Final = b"\xff\xd8\xff"


class ContextImage(WireModel):
    """One product or label image the pipeline may look at (1.2.0).

    Not `ImageRef`: that type belongs to the publishable listing and refuses `packaging_label`, and
    the label must still reach the extractor. Only base64 bytes for now; no URL field.
    """

    model_config = WireModel.model_config | ConfigDict(hide_input_in_errors=True)

    image_id: UUID
    slot: ImageSlot
    colourway_id: UUID | None = None
    width: Annotated[int, Field(ge=1, le=MAX_CONTEXT_IMAGE_PX)]
    height: Annotated[int, Field(ge=1, le=MAX_CONTEXT_IMAGE_PX)]
    # Kept out of repr and of validation errors: the bytes never reach a log line.
    data_b64: Annotated[str, Field(repr=False)]

    @field_validator("data_b64", mode="plain")
    @classmethod
    def _jpeg_base64(cls, value: object) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError("data_b64 must be a non-empty base64 string")
        if len(value) > MAX_CONTEXT_IMAGE_B64_CHARS:
            raise ValueError(f"data_b64 exceeds {MAX_CONTEXT_IMAGE_B64_CHARS} characters")
        try:
            head = b64decode(value[:8], validate=True)
            b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("data_b64 is not valid base64") from None
        if not head.startswith(_JPEG_MAGIC):
            raise ValueError("data_b64 is not a JPEG")
        return value


def _unique_images(images: list[ContextImage]) -> list[ContextImage]:
    ids = [image.image_id for image in images]
    if len(set(ids)) != len(ids):
        raise ValueError("an image appears more than once")
    return images


class GenerationContext(WireModel):
    schema_version: str = SCHEMA_VERSION
    request_id: RequestId
    job_id: UUID
    tenant_id: UUID
    product_id: UUID
    listing_id: UUID
    marketplace: Marketplace
    category: Slug
    product: dict[str, Any]
    brand_profile: dict[str, Any]
    marketplace_rules: RuleSet
    category_template: CategoryTemplate
    previous_version: CanonicalListing | None = None
    user_instructions: str | None = None
    max_attempts: Annotated[int, Field(ge=1, le=3)] = 3
    locale: Locale = "en-IN"
    review_feedback: Annotated[list[NonBlankStr], Field(max_length=5)] = Field(default_factory=list)
    # 1.2.0, filled by the backend: per product key, where its value came from.
    product_provenance: dict[str, Provenance] = Field(default_factory=dict)
    # 1.2.0: keys the conflict scan found disagreeing; absent means no conflict questions.
    conflicts: list[FactConflict] = Field(default_factory=list)
    # 1.2.0: dotted paths into `product` the seller just answered (e.g. attributes.fiber_composition).
    answered_keys: list[str] = Field(default_factory=list)
    # 1.2.0: product and label images for vision, at most 6 product images plus the label (C11).
    images: Annotated[list[ContextImage], Field(max_length=MAX_CONTEXT_IMAGES)] = Field(
        default_factory=list
    )

    @field_validator("images")
    @classmethod
    def _images_unique(cls, images: list[ContextImage]) -> list[ContextImage]:
        return _unique_images(images)

    @field_validator("review_feedback")
    @classmethod
    def _feedback_len(cls, items: list[str]) -> list[str]:
        for item in items:
            if len(item) > 2000:
                raise ValueError("review_feedback item too long")
        return items

    @model_validator(mode="after")
    def _seam_and_template(self) -> GenerationContext:
        walk_seam_dicts_and_text(self)
        for spec in self.category_template.specs.values():
            if spec.marketplace_field is not None or spec.value_map:
                raise ValueError("adapter-populated category_template forbidden at seam")
        return self


# 1.2.0: what the worker stores as a fact task (`human_tasks`): `kind` is one of its fact kinds and
# `target_path` passes `ck_human_tasks_target_path` (0002_listing_pipeline.py).
QuestionKind = Literal["missing_fact", "conflicting_facts", "unverified_declaration"]
# `account.*` passes the target-path check but not `ck_human_tasks_fact_target`, which refuses it for
# every fact kind, so a question can only target a product or brand fact.
QUESTION_TARGET_PATH: Final = r"^(facts|brand)(\.[a-z0-9_]{1,64}){1,2}$"
MAX_RESULT_QUESTIONS: Final = 20
MAX_RESULT_CHANGES: Final = 200
MAX_QUESTION_EVIDENCE: Final = 20


class Question(WireModel):
    """One plain question for the seller about a missing, conflicting or unverified fact (1.2.0)."""

    kind: QuestionKind
    target_path: Annotated[str, Field(pattern=QUESTION_TARGET_PATH)]  # facts.attributes.<key>
    question: Annotated[str, Field(min_length=5, max_length=300)]
    reason: Annotated[str, Field(min_length=5, max_length=500)]
    suggestions: Annotated[list[Annotated[NonBlankStr, Field(max_length=120)]], Field(max_length=6)]
    # "image:<uuid>", "import_row:<id>", "agent_run:<uuid>"
    evidence: Annotated[
        list[Annotated[NonBlankStr, Field(max_length=128)]],
        Field(max_length=MAX_QUESTION_EVIDENCE),
    ]


class FieldChange(WireModel):
    """One field a regeneration changed, for the agent log (1.2.0)."""

    field: str
    before: AttributeValue | None  # text, or an attribute value
    after: AttributeValue | None
    reason: Annotated[str, Field(max_length=300)]

    @field_validator("field")
    @classmethod
    def _field_path(cls, value: str) -> str:
        if not is_valid_field_path(value):
            raise ValueError(f"{value[:64]!r} is not a field path")
        return value


class AgentBlockStatus(WireModel):
    agent: Literal["content", "keyword", "attribute", "image"]
    status: Literal["ok", "failed", "skipped"]
    agent_run_id: UUID | None = None
    error: ErrorReport | None = None


class GenerationResult(WireModel):
    """Return value of run_listing_pipeline (implemented in mkos_pipeline.graph)."""

    schema_version: str = SCHEMA_VERSION
    request_id: RequestId
    job_id: UUID
    listing: CanonicalListing | None = None
    image_reports: list[ImageReport] = Field(default_factory=list)
    validation: ValidationSummary | None = None
    agent_runs: list[AgentRunRecord] = Field(default_factory=list)
    blocks: list[AgentBlockStatus] = Field(default_factory=list)
    attempts_used: Annotated[int, Field(ge=0, le=3)] = 0
    terminal_state: TerminalState
    error: ErrorReport | None = None
    questions: Annotated[list[Question], Field(max_length=MAX_RESULT_QUESTIONS)] = Field(
        default_factory=list
    )  # 1.2.0
    changes: Annotated[list[FieldChange], Field(max_length=MAX_RESULT_CHANGES)] = Field(
        default_factory=list
    )  # 1.2.0
