from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field

from mkos_schemas._base import WireModel
from mkos_schemas.enums import Marketplace, Severity
from mkos_schemas.listing import CategorySlug

RuleType = Literal[
    "max_length",
    "min_length",
    "banned_terms",
    "required_attribute",
    "max_items",
    "min_items",
    "allowed_values",
    "conditional_required",
    "cross_field",
    "claim_requires_attribute",
    "image_requirement",
    "pattern",
    "fact_source",
    "fact_backed_attribute",
]


class Rule(WireModel):
    field: str
    severity: Severity = "error"
    enabled: bool = True
    rule_type: RuleType | None = None
    rule_def: dict[str, Any] = Field(default_factory=dict)
    id: UUID | None = None
    subject: str = ""
    pack: str = "core"
    version: int = 1


class AttributeSpec(WireModel):
    requirement: Literal["required", "optional", "conditional", "unsupported"] | None = None
    value_type: str | None = None
    allowed_values: list[str] = Field(default_factory=list)
    max_length: int | None = None
    unit: str | None = None
    when: dict[str, Any] | None = None


class CategoryTemplate(WireModel):
    marketplace: Marketplace
    category: CategorySlug
    external_category_id: str | None = None
    required_attributes: list[str] = Field(default_factory=list)
    optional_attributes: list[str] = Field(default_factory=list)
    conditional_attributes: list[str] = Field(default_factory=list)
    unsupported_attributes: list[str] = Field(default_factory=list)
    specs: dict[str, AttributeSpec] = Field(default_factory=dict)
    variation_themes: list[str] = Field(default_factory=list)


class RuleSet(WireModel):
    marketplace: Marketplace
    category: CategorySlug
    rules: list[Rule] = Field(default_factory=list)
    rules_version: int = 1
    loaded_at: datetime
