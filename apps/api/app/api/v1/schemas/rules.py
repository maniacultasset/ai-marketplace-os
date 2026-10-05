"""Rule list models (owner step S14b). The dry-run answer is `mkos_schemas.ValidationResult`."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from mkos_schemas.base import Slug, WireModel
from mkos_schemas.enums import Marketplace, Severity
from pydantic import Field

from app.api.pagination import Page

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


class RuleOut(WireModel):
    id: UUID
    marketplace: Marketplace
    category: Slug | None = None
    field: str
    rule_type: RuleType
    severity: Severity
    version: Annotated[int, Field(ge=1)]
    enabled: bool
    scope: Literal["platform", "tenant"]


class RulePage(Page[RuleOut]):
    pass
