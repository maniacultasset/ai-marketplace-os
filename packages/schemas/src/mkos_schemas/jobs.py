"""Job envelopes and AI seam payloads."""

from __future__ import annotations

import ipaddress
from typing import Annotated, Any, Literal
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
from .enums import JobKind, JobState, Locale, Marketplace, PipelineStage
from .errors import ErrorReport
from .images import ImageReport
from .listing import CanonicalListing
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
