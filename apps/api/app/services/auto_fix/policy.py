"""The fix policy (SI4 A1; spec L1061-1067 "Auto-Fixable Classes" / "Human-Required Classes").

Every `ErrorCode` has one row: fix it deterministically, hand it to the AI for that field only,
retry it, ask the seller one question, or leave it to operations (never a listing issue).

`route` applies the **safety overrides** first. A critical dictionary key, a compliance or
commercial gate, an unknown class, an image and a variation rebuild are never fixed by code or
by the AI on their own: they become a question (T34; spec L763-765, "must not invent"). Then
the item's own `fix_action` (an error-map row) wins over the table.

Pure: no I/O, no clock, no randomness (the `auto-fix-pure` import contract).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorClass, FixAction, Gate
from mkos_schemas.errors import ErrorItem
from mkos_schemas.paths import is_valid_field_path


class Route(StrEnum):
    DETERMINISTIC = "deterministic"
    AI_FIELD = "ai_field"
    RETRY = "retry"
    ASK = "ask"
    OPS = "ops"


@dataclass(frozen=True, slots=True)
class Routing:
    route: Route
    fix_action: FixAction | None = None
    task_kind: str | None = None  # missing_fact | image_replacement | marketplace_error_review
    fallback_task_kind: str | None = None  # the question when a deterministic fix does not apply
    reason: str = "table"


# D5: a trim may remove at most this share of the slot, measured in the rule's unit.
TRIM_MAX_SHARE: Final = Decimal("0.15")

MISSING_FACT: Final = "missing_fact"
IMAGE_REPLACEMENT: Final = "image_replacement"
MARKETPLACE_ERROR_REVIEW: Final = "marketplace_error_review"

_OVERRIDE_GATES: Final = frozenset({Gate.COMPLIANCE, Gate.COMMERCIAL})
_FIXABLE_ROUTES: Final = frozenset({Route.DETERMINISTIC, Route.AI_FIELD})


def _fix(action: FixAction, fallback: str | None = None) -> Routing:
    return Routing(Route.DETERMINISTIC, fix_action=action, fallback_task_kind=fallback)


def _ask(kind: str | None) -> Routing:
    return Routing(Route.ASK, task_kind=kind)


_AI: Final = Routing(Route.AI_FIELD)
_RETRY: Final = Routing(Route.RETRY)
_OPS: Final = Routing(Route.OPS)

_E = ErrorCode
_TABLE: dict[ErrorCode, Routing] = {
    # deterministic (spec "formatting issue"; "wrong portal enum mapped from a known value")
    _E.FIELD_TOO_LONG: _fix(FixAction.REFORMAT),
    _E.BYTE_BUDGET_EXCEEDED: _fix(FixAction.REFORMAT),
    _E.KEYWORD_DUPLICATE: _fix(FixAction.REFORMAT),
    _E.VALUE_NOT_ALLOWED: _fix(FixAction.REMAP_VALUE, MISSING_FACT),
    # the AI rewrites only that field (T21)
    _E.FIELD_TOO_SHORT: _AI,
    _E.BANNED_TERM: _AI,
    _E.PATTERN_MISMATCH: _AI,
    _E.UNSUPPORTED_CLAIM: _AI,
    _E.SCHEMA_INVALID: _AI,
    # the seller answers one question (spec "missing factual product info")
    _E.MISSING_ATTRIBUTE: _ask(MISSING_FACT),
    _E.CRITICAL_FACT_MISSING: _ask(MISSING_FACT),
    _E.REQUIRED_FIELD_MISSING: _ask(MISSING_FACT),
    _E.PRICE_ABOVE_MRP: _ask(MISSING_FACT),
    _E.FACT_NEEDS_CONFIRMATION: _ask(MISSING_FACT),
    _E.FACT_MISMATCH: _ask(MISSING_FACT),
    _E.COMPLIANCE_DECLARATION_MISSING: _ask(MISSING_FACT),
    _E.IDENTIFIER_MISSING: _ask(MISSING_FACT),
    _E.IMAGE_NON_COMPLIANT: _ask(IMAGE_REPLACEMENT),
    _E.MARKETPLACE_UNKNOWN_ERROR: _ask(MARKETPLACE_ERROR_REVIEW),
    _E.API_REJECTED_PARTIAL: _ask(MARKETPLACE_ERROR_REVIEW),
    _E.PUBLISH_FAILED: _ask(MARKETPLACE_ERROR_REVIEW),
    # a human decides, with no templated question (an open issue with its suggestion)
    _E.CATEGORY_MISMATCH: _ask(None),
    _E.CROSS_FIELD_MISMATCH: _ask(None),
    _E.IDENTIFIER_DUPLICATE: _ask(None),
    _E.IDENTIFIER_INVALID: _ask(None),
    _E.SKU_CONFLICT: _ask(None),
    _E.SELLER_SKU_CONFLICT: _ask(None),
    _E.VARIANT_MATRIX_INCOMPLETE: _ask(None),
    _E.VARIATION_INVALID: _ask(None),
    # a known retryable condition (spec L1344-1358 "Anti-Loop Rule")
    _E.RATE_LIMITED: _RETRY,
    _E.MARKETPLACE_UNAVAILABLE: _RETRY,
    _E.SUBMISSION_NOT_APPLIED: _RETRY,
    _E.MARKETPLACE_UNCONFIRMED: _RETRY,
    _E.TIMEOUT: _RETRY,
    # operations: API, auth, account, job, import and system codes (never a listing issue)
    _E.ACCOUNT_PERMISSION_DENIED: _OPS,
    _E.AUTH_EXPIRED: _OPS,
    _E.BATCH_TOO_LARGE: _OPS,
    _E.BUDGET_EXCEEDED: _OPS,
    _E.BULK_APPROVE_REQUIRES_VALIDATED: _OPS,
    _E.CATEGORY_NOT_MAPPED: _OPS,
    _E.DUPLICATE_SKU_IN_FILE: _OPS,
    _E.DUPLICATE_SUBMISSION_BLOCKED: _OPS,
    _E.FACTS_CHANGED_SINCE_APPROVAL: _OPS,
    _E.FACT_LEVEL_INVALID: _OPS,
    _E.FACT_SOURCE_FORBIDDEN: _OPS,
    _E.GENERATION_FAILED: _OPS,
    _E.HEADER_DUPLICATE_COLUMN: _OPS,
    _E.HEADER_MISSING_COLUMN: _OPS,
    _E.HEADER_UNKNOWN_COLUMN: _OPS,
    _E.IDEMPOTENCY_KEY_REUSED: _OPS,
    _E.IMPORT_FAILED: _OPS,
    _E.INTERNAL_ERROR: _OPS,
    _E.INVALID_STATUS_TRANSITION: _OPS,
    _E.JOB_TIMEOUT: _OPS,
    _E.JSON_INVALID: _OPS,
    _E.LAST_OWNER_REQUIRED: _OPS,
    _E.LISTING_ARCHIVED: _OPS,
    _E.LISTING_BUSY: _OPS,
    _E.LISTING_PAUSED: _OPS,
    _E.MAKER_CHECKER_VIOLATION: _OPS,
    _E.MARKETPLACE_ACCOUNT_CONFLICT: _OPS,
    _E.METHOD_NOT_ALLOWED: _OPS,
    _E.MFA_ENROLLMENT_REQUIRED: _OPS,
    _E.MFA_STEP_UP_REQUIRED: _OPS,
    _E.MODEL_UNAVAILABLE: _OPS,
    _E.NOT_FOUND: _OPS,
    _E.NOT_IMPLEMENTED: _OPS,
    _E.OAUTH_STATE_INVALID: _OPS,
    _E.PAYLOAD_TOO_LARGE: _OPS,
    _E.PERMISSION_DENIED: _OPS,
    _E.RETRY_EXHAUSTED: _OPS,
    _E.STALE_VERSION: _OPS,
    _E.SUBMISSION_ATTEMPTS_EXHAUSTED: _OPS,
    _E.SUBMISSION_IN_FLIGHT: _OPS,
    _E.TOKEN_LIMIT: _OPS,
    _E.UNAUTHENTICATED: _OPS,
    _E.UNKNOWN_FACT_KEY: _OPS,
    _E.VALIDATION_FAILED: _OPS,
}
POLICY: Final[Mapping[ErrorCode, Routing]] = MappingProxyType(_TABLE)

_FACT_ROOTS: Final = ("attributes.", "facts.")


def fact_key(field: str) -> str | None:
    """`attributes.<k>`, `facts.<k>` or `variants[<sku>].facts.<k>` → `<k>`, else None."""
    if not is_valid_field_path(field):
        return None
    if field.startswith("variants["):
        head, sep, key = field.partition("].facts.")
        return key if sep and head else None
    for root in _FACT_ROOTS:
        if field.startswith(root):
            return field.removeprefix(root)
    return None


def _override(item: ErrorItem, source: str, critical_keys: frozenset[str]) -> Routing | None:
    """The first safety override that applies, as the question it becomes, else None."""
    key = fact_key(item.field)
    if key is not None and key in critical_keys:
        return Routing(Route.ASK, task_kind=MISSING_FACT, reason="critical_key")
    if item.gate is not None and item.gate in _OVERRIDE_GATES:
        return Routing(Route.ASK, task_kind=None, reason=f"gate:{item.gate.value}")
    is_image = (
        item.error_code is ErrorCode.IMAGE_NON_COMPLIANT
        or item.field == "images"
        or item.field.startswith("images[")
        or source == "vision_model"
    )
    if is_image:
        return Routing(Route.ASK, task_kind=IMAGE_REPLACEMENT, reason="image")
    if item.error_class is ErrorClass.UNKNOWN:
        return Routing(Route.ASK, task_kind=MARKETPLACE_ERROR_REVIEW, reason="class_unknown")
    if item.fix_action is FixAction.REBUILD_VARIATION:
        return Routing(Route.ASK, task_kind=None, reason="rebuild_variation")
    return None


def override_reason(item: ErrorItem, *, source: str, critical_keys: frozenset[str]) -> str | None:
    """The safety override that applies to ``item`` (a machine token), else None. A stored issue
    row hit by one is never shown as auto-fixable, whatever the item or the error map says."""
    found = _override(item, source, critical_keys)
    return None if found is None else found.reason


def _base(item: ErrorItem) -> Routing:
    row = POLICY[item.error_code]
    if item.fix_action is not None:
        fallback = row.fallback_task_kind or row.task_kind
        return Routing(
            Route.DETERMINISTIC,
            fix_action=item.fix_action,
            fallback_task_kind=fallback,
            reason="item",
        )
    return row


def route(item: ErrorItem, *, source: str, critical_keys: frozenset[str]) -> Routing:
    """The route of one item: safety overrides, then the item's `fix_action`, then `POLICY`."""
    base = _base(item)
    override = _override(item, source, critical_keys)
    if override is not None and (base.route in _FIXABLE_ROUTES or base.route is Route.ASK):
        if base.route is Route.ASK and base.task_kind is not None:
            return Routing(Route.ASK, task_kind=base.task_kind, reason=override.reason)
        return override
    return base


def ask_kind(r: Routing) -> str | None:
    """The question an open row becomes: its task kind when asked, its fallback when the
    deterministic fix did not apply (or was never tried), else None."""
    if r.route is Route.ASK:
        return r.task_kind
    if r.route is Route.DETERMINISTIC:
        return r.fallback_task_kind
    return None
