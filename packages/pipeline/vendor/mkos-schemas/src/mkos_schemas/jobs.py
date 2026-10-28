from typing import Any
from uuid import UUID

from pydantic import Field

from mkos_schemas._base import WireModel
from mkos_schemas.agent import AgentBlockStatus, AgentRunRecord
from mkos_schemas.enums import Locale, Marketplace, TerminalState
from mkos_schemas.errors import ErrorReport
from mkos_schemas.images import ImageReport
from mkos_schemas.listing import CanonicalListing, CategorySlug
from mkos_schemas.rules import CategoryTemplate, RuleSet
from mkos_schemas.validation import ValidationSummary


class GenerationContext(WireModel):
    schema_version: str = "1.2.0"
    request_id: str = Field(min_length=1, max_length=128)
    job_id: UUID
    tenant_id: UUID
    product_id: UUID
    listing_id: UUID
    marketplace: Marketplace
    category: CategorySlug
    product: dict[str, Any] = Field(default_factory=dict)
    brand_profile: dict[str, Any] = Field(default_factory=dict)
    marketplace_rules: RuleSet
    category_template: CategoryTemplate
    previous_version: CanonicalListing | None = None
    user_instructions: str | None = None
    max_attempts: int = Field(default=3, ge=1, le=3)
    locale: Locale = "en-IN"
    review_feedback: list[str] = Field(default_factory=list, max_length=5)


class GenerationResult(WireModel):
    schema_version: str = "1.2.0"
    request_id: str = Field(min_length=1, max_length=128)
    job_id: UUID
    listing: CanonicalListing | None = None
    image_reports: list[ImageReport] = Field(default_factory=list)
    validation: ValidationSummary | None = None
    agent_runs: list[AgentRunRecord] = Field(default_factory=list, max_length=100)
    blocks: list[AgentBlockStatus] = Field(default_factory=list)
    attempts_used: int = Field(default=0, ge=0, le=3)
    terminal_state: TerminalState
    error: ErrorReport | None = None
