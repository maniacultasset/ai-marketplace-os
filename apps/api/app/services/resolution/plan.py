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
5. the SI4 fix policy (`policy.route`, safety overrides first): DETERMINISTIC -> `seller`
   (`auto_content` stays declared but unplanned until its writer, the content auto-fix T15,
   is built: fix round 3); AI_FIELD -> `ai_field` when the AI is allowed and the path is not
   critical, else `seller`; ASK -> `seller` (a fact or an image), `staff` (an unknown
   marketplace error); RETRY -> `retry`; OPS -> `staff`.
6. a `seller` step only where the inbox can ask (`seller_answers`: one image, or a fact key it
   answers, CP-D25); otherwise `staff`: `content_review` on a content or keyword path, `mapping`
   on `listing`, else `unanswerable_fact`.

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

Lane = Literal["auto_repair", "auto_content", "ai_field", "seller", "marketplace", "retry", "staff"]
Cause = Literal["seller_fact", "ai_content", "mapping", "image", "marketplace", "unknown"]
AUTOMATIC: Final = frozenset({"auto_repair", "auto_content", "ai_field", "retry"})
FIXED_RESOLUTIONS: Final = frozenset({"deterministic_fix", "ai_correction"})
QUALIFICATION_CODES: Final = frozenset({"99320", "5665", "5461", "8541", "8542"})
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
    klass = _klass(issue.error_class)
    if klass is ErrorClass.ACCOUNT_PERMISSION or issue.marketplace_code in QUALIFICATION_CODES:
        return "marketplace", "permission"
    if issue.id in ctx.repairable:
        return "auto_repair", "repair"
    routing = route(_item(issue), source=issue.source, critical_keys=ctx.critical_keys)
    path = issue.field_path
    if routing.route is Route.DETERMINISTIC:
        # fix round 3: `auto_content` waits for its writer (the content auto-fix, T15, not built):
        # until then a content or keyword path is a staff content review, never a silent lane
        return _seller_or_staff(path)
    if routing.route is Route.AI_FIELD:
        key = path.split(".", 1)[1] if "." in path else path
        if ctx.ai_allowed and key not in ctx.critical_keys and not path.startswith("images"):
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


def plan(issues: Sequence[IssueView], ctx: PlanContext) -> tuple[Step, ...]:
    steps: list[Step] = []
    for issue in issues:
        found = _lane(issue, ctx)
        if found is None:
            continue
        lane, action = found
        steps.append(Step(issue.id, lane, cause_of(issue, lane, ctx.provenance), action))
    return tuple(steps)


def lane_for(error_code: str, error_class: str, source: str = "amazon_adapter") -> Lane:
    """The lane an issue of this code and class takes with no listing context (the catalogue)."""
    view = IssueView(
        id=uuid.UUID(int=0),
        source=source,
        field_path="listing",
        error_code=error_code,
        error_class=error_class,
        severity="error",
    )
    found = _lane(view, PlanContext())
    return "staff" if found is None else found[0]


def label(issue: IssueView, ctx: PlanContext) -> tuple[Lane, Cause] | None:
    """The lane and cause the planner gives one issue (None for a WARNING): the resolution read
    and the inbox label each row with the same rules the executor plans with (T20)."""
    found = _lane(issue, ctx)
    if found is None:
        return None
    return found[0], cause_of(issue, found[0], ctx.provenance)
