"""Builders for Rules Engine tests (S14a). Fixed ids and times keep every report reproducible."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from mkos_schemas.jobs import CategoryTemplate
from mkos_schemas.listing import CanonicalListing
from mkos_schemas.rules import Rule, RuleSet

PRODUCT_ID = UUID("00000000-0000-0000-0000-0000000000aa")
LISTING_ID = UUID("00000000-0000-0000-0000-0000000000bb")
TENANT_ID = UUID("00000000-0000-0000-0000-0000000000cc")
LOADED_AT = datetime(2026, 10, 5, tzinfo=UTC)
CATEGORY = "ethnic_wear_kurta_sets"
FIXTURES = next(
    p / "packages/schemas/fixtures"
    for p in Path(__file__).resolve().parents
    if (p / "packages/schemas/fixtures").is_dir()
)
"""The contracts fixtures, found upwards (the tests also run from a mutmut copy)."""

_ids = itertools.count(1)


def next_id() -> UUID:
    return UUID(int=0x5140_0000_0000_0000_0000_0000_0000_0000 + next(_ids))


def listing(**over: Any) -> CanonicalListing:
    content = {
        "title": "Cotton kurta set for women",
        "bullets": ["Soft cotton fabric", "Machine washable"],
        "description": "A comfortable cotton kurta set.",
    }
    content.update(over.pop("content", {}))
    data: dict[str, Any] = {
        "listing_id": LISTING_ID,
        "product_id": PRODUCT_ID,
        "marketplace": "amazon",
        "category": CATEGORY,
        "content": content,
        "attributes": {"color": "black", "fabric_type": "cotton"},
    }
    data.update(over)
    return CanonicalListing.model_validate(data)


def rule(rule_def: dict[str, Any], **over: Any) -> Rule:
    data: dict[str, Any] = {
        "id": over.pop("id", None) or next_id(),
        "marketplace": "amazon",
        "category": CATEGORY,
        "field": "content.title",
        "rule_def": {"message": "rule failed {value}", **rule_def},
    }
    data.update(over)
    return Rule.model_validate(data)


def ruleset(*rules: Rule, **over: Any) -> RuleSet:
    data: dict[str, Any] = {
        "marketplace": "amazon",
        "category": CATEGORY,
        "rules": list(rules),
        "rules_version": 1,
        "loaded_at": LOADED_AT,
    }
    data.update(over)
    return RuleSet.model_validate(data)


def template(**over: Any) -> CategoryTemplate:
    data: dict[str, Any] = {"marketplace": "amazon", "category": CATEGORY}
    data.update(over)
    return CategoryTemplate.model_validate(data)
