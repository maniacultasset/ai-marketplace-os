"""A rule is a ROW, not code. `rules.rule_def` is exactly one of the models
below, discriminated on `type`. RulesEngine.validate(listing, ruleset) is a pure
function: no DB session, no LLM, no network. Adding a marketplace cap is an
INSERT, not a deploy.

Two columns the 13-table DDL omits and migration 0002 must add:
  tenant_id UUID NULL REFERENCES tenants(id)   -- NULL = platform rule
  enabled   BOOLEAN NOT NULL DEFAULT true
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from .base import WireModel
from .codes import ErrorCode
from .enums import Marketplace, Severity

# `field` is a paths.py path. Two extensions, and no others:
#   "content.bullets[*]"  -> every element; the engine emits the concrete index
#   "*"                   -> every text-bearing path (title, bullets[*], description)
FIELD_WILDCARD = "*"


class _RuleBase(WireModel):
    message: str          # shown to the reviewer; {value}, {limit}, {term} interpolated
    suggestion: str | None = None   # passed VERBATIM to the correction agent


class MaxLength(_RuleBase):
    type: Literal["max_length"] = "max_length"
    limit: int = Field(gt=0)
    unit: Literal["chars", "bytes", "words"] = "chars"
    error_code: ErrorCode = ErrorCode.FIELD_TOO_LONG


class MinLength(_RuleBase):
    type: Literal["min_length"] = "min_length"
    limit: int = Field(gt=0)
    unit: Literal["chars", "bytes", "words"] = "chars"
    error_code: ErrorCode = ErrorCode.FIELD_TOO_SHORT


class BannedTerms(_RuleBase):
    type: Literal["banned_terms"] = "banned_terms"
    terms: list[str] = Field(min_length=1)
    match: Literal["word", "substring", "regex"] = "word"
    case_sensitive: bool = False
    error_code: ErrorCode = ErrorCode.BANNED_TERM


class RequiredAttribute(_RuleBase):
    type: Literal["required_attribute"] = "required_attribute"
    attribute: str = Field(pattern=r"^[a-z0-9_]+$")
    allowed_values: list[str] | None = None   # non-null also enforces membership
    error_code: ErrorCode = ErrorCode.MISSING_ATTRIBUTE


class MaxItems(_RuleBase):
    type: Literal["max_items"] = "max_items"
    limit: int = Field(gt=0)
    unique: bool = False                     # true -> duplicates are KEYWORD_DUPLICATE
    error_code: ErrorCode = ErrorCode.FIELD_TOO_LONG


RuleDef = Annotated[
    MaxLength | MinLength | BannedTerms | RequiredAttribute | MaxItems,
    Field(discriminator="type"),
]


class Rule(WireModel):
    """One row of `rules`. UNIQUE (marketplace, category, field, version)."""
    id: UUID
    tenant_id: UUID | None = None      # NULL = platform-wide; Phase-2 override hook
    marketplace: Marketplace
    category: str | None = None        # NULL = every category on that marketplace
    field: str
    severity: Severity = Severity.ERROR
    version: int = 1
    enabled: bool = True
    rule_def: RuleDef


class RuleSet(WireModel):
    """What PipelineDeps.load_rules() returns. Resolution order: a tenant rule
    shadows the platform rule with the same (marketplace, category, field), and
    a category rule shadows the category=NULL rule."""
    marketplace: Marketplace
    category: str
    rules: list[Rule] = []
    rules_version: int = 1             # pinned onto validation_runs.rules_version
    loaded_at: datetime | None = None
