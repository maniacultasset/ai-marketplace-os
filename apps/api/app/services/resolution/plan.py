"""The resolution planner (SI11 T11; ADR-0028 proposed; P:B1, D16; spec L1061-1067, L1336-1358).
Pure: no I/O, no clock, no adapter (the `resolution-plan-pure` import contract).

`plan(issues, ctx)` gives each open blocking issue one lane, first match wins:

1. a WARNING never makes a step;
2. a recurrence of a key Kuinn already fixed (`deterministic_fix`, `ai_correction`) -> `staff`
   (spec L1356 "stop repeated retries and escalate"; one try per key); likewise an issue on an
   attribute whose live repair the marketplace refused under another code
   (`ctx.repair_rejected`, action `repair_rejected`): one automatic guess per attribute;
3. an account-permission class or a qualification code -> `marketplace` (only the marketplace can
   approve a brand or category);
4. an issue the marketplace's repairer could fix (`ctx.repairable`) -> `auto_repair` (the
   repairer already refused text, price, MRP and party values);
5. the SI4 fix policy (`policy.route`, safety overrides first): DETERMINISTIC -> `auto_content`
   on a content or keyword path the content fix can change (`ctx.content_fixable`: the
   executor ran the same pure fixer first, T15), else `seller` (fix round 3: never a lane with
   nothing behind it); AI_FIELD -> `ai_field` when the AI is allowed (`ctx.ai_allowed`: SI10's
   credit gate admits), the path is not critical, the row is the marketplace's and the merge can
   keep only that path (`merge.mergeable`), else `seller`; ASK -> `seller` (a fact or an image), `staff` (an unknown
   marketplace error); RETRY -> `retry`; OPS -> `staff`.
6. a `seller` step only where the inbox can ask (`seller_answers`: one image, or a fact key it
   answers, CP-D25); otherwise `staff`: `content_review` on a content or keyword path, `mapping`
   on `listing`, else `unanswerable_fact`.

SI13 (T7; CP-D8) adds, after rule 2 and before rule 3:
- an adapter `SKU_CONFLICT`/`SELLER_SKU_CONFLICT`, or a locked-SKU code (`LOCKED_SKU_CODES`) ->
  `staff` `sku_conflict` (a SKU cannot be renamed on Amazon; never a repair, never the seller);
- an adapter `CATEGORY_MISMATCH` -> `seller` `recategorize` when the listing can still be
  recategorised (`ctx.recategorizable`: never live, failed or never seen, no live variants), else
  `staff` `recategorize_review`;
- an adapter `IDENTIFIER_DUPLICATE`, `IDENTIFIER_INVALID` or `IDENTIFIER_MISSING` (item 13: a
  barcode of another brand, an invalid barcode, or no barcode and no approved GTIN exemption) on
  exactly `facts.identifier` or `variants[<sku>].facts.identifier` -> `seller` `edit_identifier`.
The two seller actions open no inbox question: the issue row is the seller's signal and the action
is on the listing (`SELLER_ACTIONS`). And a plan-level hold: while an adapter `CATEGORY_MISMATCH`
ERROR is planned, adapter `MISSING_ATTRIBUTE` rows get no step (one ask, not ten).

`cause` says whose value was wrong (P:D16). `lane_for(code, class)` is the issue-free lane of the
error catalogue (T19).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorClass, ErrorSource, Gate, Severity
from mkos_schemas.errors import ErrorItem
from mkos_schemas.paths import is_valid_field_path

from app.services.auto_fix.policy import (
    IMAGE_REPLACEMENT,
    MARKETPLACE_ERROR_REVIEW,
    MISSING_FACT,
    Route,
    fact_key,
    route,
)
from app.services.resolution.merge import mergeable

Lane = Literal["auto_repair", "auto_content", "ai_field", "seller", "marketplace", "retry", "staff"]
Cause = Literal["seller_fact", "ai_content", "mapping", "image", "marketplace", "unknown"]
AUTOMATIC: Final = frozenset({"auto_repair", "auto_content", "ai_field", "retry"})
FIXED_RESOLUTIONS: Final = frozenset({"deterministic_fix", "ai_correction"})
# 8542 is a catalog conflict on a matched barcode, not an approval: it takes the identifier lanes
QUALIFICATION_CODES: Final = frozenset({"99320", "5665", "5461", "8541"})
# SI13: Amazon's codes for a SKU whose identity is locked (F:62): a SKU cannot be renamed
LOCKED_SKU_CODES: Final = frozenset({"4005013", "101168"})
SKU_CODES: Final = frozenset({ErrorCode.SKU_CONFLICT.value, ErrorCode.SELLER_SKU_CONFLICT.value})
# SI13 (CP-D8): seller steps that open no inbox question (the action is on the listing)
SELLER_ACTIONS: Final = frozenset({"recategorize", "edit_identifier", "edit_content"})
_IDENTIFIER_PATH: Final = re.compile(r"(variants\[[^\]]+\]\.)?facts\.identifier")
# The marketplace's barcode refusals the seller fixes at the product's barcode choice
IDENTIFIER_EDIT_CODES: Final = frozenset(
    {
        ErrorCode.IDENTIFIER_DUPLICATE.value,
        ErrorCode.IDENTIFIER_INVALID.value,
        ErrorCode.IDENTIFIER_MISSING.value,
    }
)
ANSWERABLE_FACTS: Final = ("facts.gender", "facts.brand_name")
ADAPTER_SOURCES: Final = frozenset({"amazon_adapter", "flipkart_adapter"})
_CONTENT_ROOTS: Final = ("content.", "keywords.")
_FACT_ROOTS: Final = ("facts.", "attributes.")
_IMAGE_PATH: Final = re.compile(r"images\[\d+\]")


@dataclass(frozen=True, slots=True)
class IssueView:
    id: uuid.UUID
    source: str
    field_path: str
    error_code: str
    error_class: str
    severity: str
    marketplace_code: str | None = None
    channel: Literal["live", "preview"] = "live"
    gate: str | None = None
    recurrence_resolution: str | None = None
    """The resolution of the closed row this one recurs (`recurrence_of_id`), if any."""


@dataclass(frozen=True, slots=True)
class PlanContext:
    critical_keys: frozenset[str] = frozenset()
    provenance: Mapping[str, str] = field(default_factory=dict)
    repairable: frozenset[uuid.UUID] = frozenset()
    repair_rejected: frozenset[uuid.UUID] = frozenset()
    approved_current: bool = False
    cycles_used: int = 0
    ai_allowed: bool = False
    content_fixable: frozenset[uuid.UUID] = frozenset()
    """Content and keyword issues the deterministic content fix can change (T15)."""
    recategorizable: bool = False
    """SI13: the listing may still be recategorised by the seller (failed, never live, no
    family with live variants, no other listing of the product the marketplace has seen)."""


@dataclass(frozen=True, slots=True)
class Step:
    issue_id: uuid.UUID
    lane: Lane
    cause: Cause
    action: str


def _code(value: str) -> ErrorCode:
    try:
        return ErrorCode(value)
    except ValueError:
        return ErrorCode.MARKETPLACE_UNKNOWN_ERROR


def _klass(value: str) -> ErrorClass:
    try:
        return ErrorClass(value)
    except ValueError:
        return ErrorClass.UNKNOWN


def _item(issue: IssueView) -> ErrorItem:
    gate: Gate | None = None
    if issue.gate is not None:
        try:
            gate = Gate(issue.gate)
        except ValueError:
            gate = None
    try:
        source = ErrorSource(issue.source)
    except ValueError:
        source = ErrorSource.SYSTEM
    return ErrorItem(
        field=issue.field_path if is_valid_field_path(issue.field_path) else "listing",
        error_code=_code(issue.error_code),
        severity=Severity.ERROR,
        message="-",
        source=source,
        retryable=False,
        error_class=_klass(issue.error_class),
        raw_code=issue.marketplace_code,
        gate=gate,
    )


def answerable(path: str) -> bool:
    """A fact path the seller's inbox can answer (CP-D25)."""
    return path.startswith("facts.attributes.") or path in ANSWERABLE_FACTS


def _is_fact(path: str) -> bool:
    return path.startswith(_FACT_ROOTS) or path.startswith("variants[")


def cause_of(issue: IssueView, lane: Lane, provenance: Mapping[str, str]) -> Cause:
    path = issue.field_path
    if lane == "marketplace":
        return "marketplace"
    if issue.error_code == ErrorCode.CATEGORY_MISMATCH.value and issue.source in ADAPTER_SOURCES:
        return "mapping"  # SI13: the category we chose no longer fits Amazon's product type
    if path.startswith("images"):
        return "image"
    if path.startswith(_CONTENT_ROOTS):
        return "ai_content"
    if path.startswith(_FACT_ROOTS):
        key = path.split(".", 1)[1]
        origin = provenance.get(key) or provenance.get(path)
        if origin == "ai":
            return "ai_content"
        return "seller_fact"
    if path == "listing" and issue.source in ADAPTER_SOURCES:
        return "mapping"  # no canonical path fed the attribute: our mapping, staff looks
    return "unknown"


def seller_answers(path: str) -> bool:
    """The seller inbox can take this path: one image (`images[<i>]`) or a fact whose key it
    can answer (`attributes.<k>`, or a `facts.` path `answerable` accepts, also under
    `variants[<sku>].`). Nothing else ever reaches the `seller` lane (round 4: a `listing`,
    `brand.*` or `request` path would open no question and stall)."""
    if _IMAGE_PATH.fullmatch(path):
        return True
    key = fact_key(path)
    if key is None:
        return False
    if path.startswith("attributes."):
        return True
    return answerable(f"facts.{key}")


def staff_action(path: str) -> str:
    """The staff action for a path the seller cannot answer."""
    if path == "listing":
        return "mapping"  # no canonical path fed the attribute: our mapping, staff looks
    if path.startswith(_CONTENT_ROOTS):
        return "content_review"
    return "unanswerable_fact"


def _seller_or_staff(path: str) -> tuple[Lane, str]:
    """A question the seller can answer, else staff (CP-D25: never a question the inbox cannot
    take). The inbox asks only about facts and images (`seller_answers`), so a content or
    keyword path is a staff content review and a `listing` path a staff mapping review."""
    if seller_answers(path):
        return "seller", "ask"
    return "staff", staff_action(path)


def _lane(issue: IssueView, ctx: PlanContext) -> tuple[Lane, str] | None:
    if issue.severity != "error":
        return None
    if issue.recurrence_resolution in FIXED_RESOLUTIONS:
        return "staff", "recurrence"
    if issue.id in ctx.repair_rejected:
        return "staff", "repair_rejected"
    adapter = issue.source in ADAPTER_SOURCES
    if adapter and (issue.error_code in SKU_CODES or issue.marketplace_code in LOCKED_SKU_CODES):
        return "staff", "sku_conflict"
    if adapter and issue.error_code == ErrorCode.CATEGORY_MISMATCH.value:
        if ctx.recategorizable:
            return "seller", "recategorize"
        return "staff", "recategorize_review"
    if (
        adapter
        and issue.error_code in IDENTIFIER_EDIT_CODES
        and _IDENTIFIER_PATH.fullmatch(issue.field_path)
    ):
        return "seller", "edit_identifier"
    klass = _klass(issue.error_class)
    if klass is ErrorClass.ACCOUNT_PERMISSION or issue.marketplace_code in QUALIFICATION_CODES:
        return "marketplace", "permission"
    if issue.id in ctx.repairable:
        return "auto_repair", "repair"
    routing = route(_item(issue), source=issue.source, critical_keys=ctx.critical_keys)
    path = issue.field_path
    if routing.route is Route.DETERMINISTIC:
        # T15: only an issue the content fix can change takes `auto_content` (its writer, the
        # auto-fix job); any other content or keyword path is a staff content review (round 3)
        if issue.id in ctx.content_fixable and path.startswith(_CONTENT_ROOTS):
            return "auto_content", "content_fix"
        if not adapter and path.startswith(_CONTENT_ROOTS):
            # QA: Kuinn's own check of text (a seller's edit over the limit) has no staff task
            # behind it and the executor never plans it: only the seller's next edit fixes it
            return "seller", "edit_content"
        return _seller_or_staff(path)
    if routing.route is Route.AI_FIELD:
        key = path.split(".", 1)[1] if "." in path else path
        if (
            ctx.ai_allowed
            and key not in ctx.critical_keys
            and not path.startswith("images")
            and issue.source in ADAPTER_SOURCES
            and mergeable(path)
        ):
            return "ai_field", "ai_rewrite"
        return _seller_or_staff(path)
    if routing.route is Route.ASK:
        if routing.task_kind == MARKETPLACE_ERROR_REVIEW:
            return "staff", "review"
        if routing.task_kind in (MISSING_FACT, IMAGE_REPLACEMENT, None):
            return _seller_or_staff(path)
        return "staff", "review"
    if routing.route is Route.RETRY:
        return "retry", "retry"
    return "staff", "ops"


def _holds_missing(issues: Sequence[IssueView], ctx: PlanContext) -> bool:
    """SI13: an adapter CATEGORY_MISMATCH ERROR is planned, so the burst of missing attributes
    that came with it waits for the category (one ask, not ten)."""
    return any(
        i.severity == "error"
        and i.source in ADAPTER_SOURCES
        and i.error_code == ErrorCode.CATEGORY_MISMATCH.value
        and _lane(i, ctx) is not None
        for i in issues
    )


def plan(issues: Sequence[IssueView], ctx: PlanContext) -> tuple[Step, ...]:
    steps: list[Step] = []
    hold = _holds_missing(issues, ctx)
    for issue in issues:
        if (
            hold
            and issue.source in ADAPTER_SOURCES
            and issue.error_code == ErrorCode.MISSING_ATTRIBUTE.value
        ):
            continue
        found = _lane(issue, ctx)
        if found is None:
            continue
        lane, action = found
        steps.append(Step(issue.id, lane, cause_of(issue, lane, ctx.provenance), action))
    return tuple(steps)


def lane_for(
    error_code: str,
    error_class: str,
    source: str = "amazon_adapter",
    *,
    field_path: str = "listing",
    ctx: PlanContext | None = None,
) -> Lane:
    """The lane an issue of this code and class takes at `field_path` (the map row's field
    hint, else `listing`) with no listing context unless `ctx` is given (the catalogue)."""
    view = IssueView(
        id=uuid.UUID(int=0),
        source=source,
        field_path=field_path,
        error_code=error_code,
        error_class=error_class,
        severity="error",
    )
    found = _lane(view, PlanContext() if ctx is None else ctx)
    return "staff" if found is None else found[0]


def label(issue: IssueView, ctx: PlanContext) -> tuple[Lane, Cause] | None:
    """The lane and cause the planner gives one issue (None for a WARNING): the resolution read
    and the inbox label each row with the same rules the executor plans with (T20)."""
    found = _lane(issue, ctx)
    if found is None:
        return None
    return found[0], cause_of(issue, found[0], ctx.provenance)
