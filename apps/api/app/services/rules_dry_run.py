"""`POST /api/v1/rules/dry-run` (S14b T12; D7, D8, C7): run the Rules Engine on a draft listing
for the caller's tenant and save nothing.

- One read-only unit of work loads the effective RuleSet and the current category template
  (`load_rules`, three statements). No write, no audit row, no enqueue, no idempotency row.
- No current template for the (marketplace, category) pair → 422 `VALIDATION_FAILED` on
  `listing.category` (D8), even when category-wide (`category IS NULL`) rules would match, so the
  engine never runs on an unknown category.
- The engine runs off the event loop in a worker thread under a process-wide limiter of 2 (C7):
  it is synchronous and can take up to about a second on a large RuleSet. The database session is
  closed before the thread starts; the thread gets only wire models.
- The dry-run knows no product and no account, so `product.*` and `account.*` conditions never
  match here (D7; the OpenAPI description says so).
- A stored rule that cannot run (`RuleLoadError`) or an engine exception is a 500
  `INTERNAL_ERROR` through the shared handler: never a pass.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from functools import partial
from typing import Final
from uuid import UUID

import anyio
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource
from mkos_schemas.listing import CanonicalListing
from mkos_schemas.validation import ValidationResult

from app.core.errors import InputError
from app.core.rbac import Principal
from app.repositories.uow import tenant_uow
from app.services.rules_engine import evaluate
from app.services.rules_loader import load_rules

EVAL_CONCURRENCY: Final = 2
_EVAL_LIMITER: Final = anyio.CapacityLimiter(EVAL_CONCURRENCY)
NO_LISTING_ID: Final = UUID(int=0)


async def dry_run(principal: Principal, listing: CanonicalListing) -> ValidationResult:
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        loaded = await load_rules(
            uow,
            tenant_id=principal.tenant_id,
            marketplace=listing.marketplace,
            category=listing.category,
        )
    if loaded.template is None:
        raise InputError(ErrorCode.VALIDATION_FAILED, "listing.category")
    started = time.monotonic()
    report = await anyio.to_thread.run_sync(
        partial(evaluate, listing, loaded.ruleset, template=loaded.template),
        limiter=_EVAL_LIMITER,
    )
    duration_ms = int((time.monotonic() - started) * 1000)
    return ValidationResult(
        tenant_id=principal.tenant_id,
        listing_id=listing.listing_id or NO_LISTING_ID,
        marketplace=listing.marketplace,
        source=ErrorSource.RULES_ENGINE,
        passed=report.passed,
        rules_version=loaded.ruleset.rules_version,
        duration_ms=duration_ms,
        report=report,
        created_at=datetime.now(UTC),
    )
