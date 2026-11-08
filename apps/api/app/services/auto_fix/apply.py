"""The fix pass (SI4 A4; spec L1061-1067 "apply deterministic fix; revalidate").

`auto_fix(listing, evaluate, ctx)` tries each `deterministic` item of the engine's report once,
in a fixed order, and keeps a fix only when re-evaluation shows that item gone **and** no new
blocking item (the accept rule). The Rules Engine stays the arbiter (invariant 2): a fixer only
proposes, the engine decides.

The same function runs inside the correction loop (`PipelineDeps._evaluate`) and on the
pipeline's final listing, so both give the same listing (AC7).

Pure: no I/O, no clock, no randomness. `evaluate` is the caller's engine closure.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import FixAction
from mkos_schemas.errors import ErrorItem, ErrorReport
from mkos_schemas.listing import CanonicalListing

from app.services.auto_fix.fixers import (
    FixContext,
    FixRecord,
    fix_keyword_duplicates,
    fix_reformat,
    fix_remap_value,
)
from app.services.auto_fix.policy import Route, route

MAX_FIXES_PER_PASS: Final = 20
SOURCE: Final = "rules_engine"

Key = tuple[str, str, str]
Evaluate = Callable[[CanonicalListing], ErrorReport]
FixResult = tuple[CanonicalListing, FixRecord] | None


@dataclass(frozen=True, slots=True)
class FixOutcome:
    listing: CanonicalListing
    report: ErrorReport
    records: tuple[FixRecord, ...]


def item_key(item: ErrorItem) -> Key:
    return (item.field, item.error_code.value, "" if item.rule_id is None else str(item.rule_id))


def _try(
    listing: CanonicalListing, item: ErrorItem, action: FixAction, ctx: FixContext
) -> FixResult:
    # Looked up at call time, so a test can stand in a fixer (AC4).
    if action is FixAction.REFORMAT:
        if item.error_code is ErrorCode.KEYWORD_DUPLICATE:
            return fix_keyword_duplicates(listing, item, ctx)
        return fix_reformat(listing, item, ctx)
    if action is FixAction.REMAP_VALUE:
        return fix_remap_value(listing, item, ctx)
    return None  # derive_field has no fixer in v1; rebuild_variation is always asked


def auto_fix(listing: CanonicalListing, evaluate: Evaluate, ctx: FixContext) -> FixOutcome:
    """Fix what the policy routes `deterministic`, keep only what the engine accepts."""
    report = evaluate(listing)
    candidates = sorted(report.blocking, key=item_key)
    records: list[FixRecord] = []
    for item in candidates:
        if len(records) >= MAX_FIXES_PER_PASS:
            break
        routing = route(item, source=SOURCE, critical_keys=ctx.critical_keys)
        if routing.route is not Route.DETERMINISTIC or routing.fix_action is None:
            continue
        key = item_key(item)
        if key not in {item_key(e) for e in report.errors}:
            continue  # an earlier fix on the same slot already cleared it
        proposed = _try(listing, item, routing.fix_action, ctx)
        if proposed is None:
            continue
        candidate, record = proposed
        new = evaluate(candidate)
        gone = key not in {item_key(e) for e in new.errors}
        no_new_blocking = {item_key(e) for e in new.blocking} <= {
            item_key(e) for e in report.blocking
        }
        if gone and no_new_blocking:
            listing, report = candidate, new
            records.append(record)
    return FixOutcome(listing=listing, report=report, records=tuple(records))
