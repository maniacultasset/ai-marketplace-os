"""Pure builders for the S18 result tests: a `GenerationContext` and a stub-shaped
`GenerationResult` (the in-repo stub's own helpers), no database."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from mkos_schemas.jobs import CategoryTemplate, GenerationContext, GenerationResult
from mkos_schemas.rules import RuleSet

CATEGORY = "ethnic_wear_kurta_sets"
TENANT = uuid.UUID("00000000-0000-4000-8000-0000000000a1")
OTHER_TENANT = uuid.UUID("00000000-0000-4000-8000-0000000000b2")
CLAIM = datetime(2026, 10, 5, 10, 0, tzinfo=UTC)
NOW = datetime(2026, 10, 5, 10, 5, tzinfo=UTC)


def context(**over: Any) -> GenerationContext:
    body: dict[str, Any] = {
        "request_id": "req-s18-unit",
        "job_id": uuid.UUID("00000000-0000-4000-8000-0000000000c3"),
        "tenant_id": TENANT,
        "product_id": uuid.UUID("00000000-0000-4000-8000-0000000000d4"),
        "listing_id": uuid.UUID("00000000-0000-4000-8000-0000000000e5"),
        "marketplace": "amazon",
        "category": CATEGORY,
        "product": {"sku": "KURTA-002"},
        "brand_profile": {},
        "marketplace_rules": RuleSet(marketplace="amazon", category=CATEGORY, loaded_at=CLAIM),
        "category_template": CategoryTemplate(marketplace="amazon", category=CATEGORY),
        **over,
    }
    return GenerationContext.model_validate(body)


def result(ctx: GenerationContext, *, long_title: bool = False, **over: Any) -> GenerationResult:
    from mkos_pipeline.graph import STUB_EPOCH, _agent_records, _blocks, build_listing

    records = [
        r.model_copy(update={"started_at": CLAIM, "created_at": CLAIM}) for r in _agent_records(ctx)
    ]
    assert STUB_EPOCH < CLAIM
    body: dict[str, Any] = {
        "request_id": ctx.request_id,
        "job_id": ctx.job_id,
        "listing": build_listing(ctx, long_title=long_title),
        "agent_runs": records,
        "blocks": _blocks(records),
        "attempts_used": 1,
        "terminal_state": "generated",
        **over,
    }
    return GenerationResult.model_validate(body)
