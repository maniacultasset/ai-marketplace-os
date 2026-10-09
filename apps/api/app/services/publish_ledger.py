"""Pure outcome routing of one publish attempt (S21 T5; DATABASE.md :871, :880, :890, §6.4
:1504-1532; PLAN D11; CP1, CP8). No I/O: the job turns a `ListingResult` or an adapter failure into
one `Decision`, and the persist step writes it.

`route_result(result, *, attempt)`:
- `published` → submission `accepted`, listing `publishing → published` (`submission_accepted`),
  job `succeeded`. A write never confirms `live`: an item the adapter calls live is recorded as
  `processing` until the status sync reads it (DW7, DATABASE.md:770), so "Submitted" is what the
  user sees after an accept.
- `partially_published` / `failed` → `partially_accepted` / `rejected`, listing
  `failed/publish_partial|publish_failed` (`submission_failed`), job `partial` / `failed`; except
  a blocking item of an action class (account permission, unknown, SKU conflict: DATABASE.md:890)
  → `blocked/marketplace_action_required`, and the 3rd non-success attempt →
  `blocked/publish_attempts_exhausted` (invariant 9). An unknown marketplace code (no error-map
  row) opens one `marketplace_error_review` task; an account-permission item a
  `permission_decision` task. The stored report is `retryable` only when every blocking item is
  (the 0008 `ls_guard` rule), so a mixed partial is never retried as is.

`route_error(kind, report, *, attempt)` (an `AdapterError` raised before anything was applied):
- `rate_limited` → `error RATE_LIMITED` (definitive: nothing applied), job retried; counted, so
  the 3rd is `blocked/publish_attempts_exhausted` (D11);
- `auth_expired` → `error AUTH_EXPIRED`, `blocked/marketplace_action_required`, the issue linked to
  S15's reconnect task; `permission_denied` → `error ACCOUNT_PERMISSION_DENIED`, blocked, a
  `permission_decision` task;
- `ambiguous` / `unavailable` → the row stays `pending` and the job is retried on the same row and
  key (CP1): no listing event.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from mkos_schemas.codes import ERROR_CODE_META, ErrorCode
from mkos_schemas.enums import ErrorClass, ItemMarketplaceState
from mkos_schemas.errors import ErrorItem, ErrorReport
from mkos_schemas.publish import ListingResult

Outcome = Literal["accepted", "partially_accepted", "rejected", "error", "pending"]
JobStatus = Literal["succeeded", "partial", "failed", "retry"]
Task = Literal["marketplace_error_review", "permission_decision", "link_reconnect"]
ErrorKind = Literal["rate_limited", "auth_expired", "permission_denied", "ambiguous", "unavailable"]

MAX_ATTEMPTS: Final = 3
ACTION_CLASSES: Final = frozenset({ErrorClass.ACCOUNT_PERMISSION, ErrorClass.UNKNOWN})
ACTION_CODES: Final = frozenset({ErrorCode.SELLER_SKU_CONFLICT, ErrorCode.SKU_CONFLICT})
UNKNOWN_CODE: Final = ErrorCode.MARKETPLACE_UNKNOWN_ERROR
EXHAUSTED: Final = ("submission_attempts_exhausted", "blocked", "publish_attempts_exhausted")
ACTION: Final = ("marketplace_action_required", "blocked", "marketplace_action_required")


@dataclass(frozen=True, slots=True)
class FieldRow:
    seller_sku: str
    field: str
    status: Literal["succeeded", "failed", "skipped"]
    error: ErrorItem | None = None


@dataclass(frozen=True, slots=True)
class ItemState:
    seller_sku: str
    state: ItemMarketplaceState
    external_id: str | None = None


@dataclass(frozen=True, slots=True)
class Decision:
    outcome: Outcome
    job_status: JobStatus
    error_code: ErrorCode | None = None
    report: ErrorReport | None = None
    listing_event: str | None = None
    listing_to: str | None = None
    reason: str | None = None
    open_task: Task | None = None
    field_rows: tuple[FieldRow, ...] = ()
    items: tuple[ItemState, ...] = ()
    submission_ref: str | None = None
    external_id: str | None = None


def item_class(item: ErrorItem) -> ErrorClass | None:
    if item.error_class is not None:
        return item.error_class
    meta = ERROR_CODE_META.get(item.error_code)
    return None if meta is None else meta.error_class


def needs_action(item: ErrorItem) -> bool:
    """A blocking item a human must resolve at the marketplace (DATABASE.md:890)."""
    return item.error_code in ACTION_CODES or item_class(item) in ACTION_CLASSES


def stored_report(report: ErrorReport) -> ErrorReport:
    """The report as the ledger stores it: `retryable` iff every blocking item is retryable."""
    blocking = report.blocking
    retryable = bool(blocking) and all(i.retryable for i in blocking)
    return report.model_copy(update={"retryable": retryable})


def _written_state(state: ItemMarketplaceState) -> ItemMarketplaceState:
    # only a status read confirms live (DW7)
    return ItemMarketplaceState.PROCESSING if state is ItemMarketplaceState.LIVE else state


def _rows(result: ListingResult) -> tuple[tuple[FieldRow, ...], tuple[ItemState, ...]]:
    rows: list[FieldRow] = []
    items: list[ItemState] = []
    for item in result.items:
        items.append(
            ItemState(item.seller_sku, _written_state(item.marketplace_state), item.external_id)
        )
        seen: set[str] = set()
        for one in item.fields:
            if one.field in seen:
                continue
            seen.add(one.field)
            rows.append(FieldRow(item.seller_sku, one.field, one.status, one.error))
    return tuple(rows), tuple(items)


def _task(blocking: Sequence[ErrorItem]) -> Task | None:
    if any(i.error_code is UNKNOWN_CODE for i in blocking):
        return "marketplace_error_review"
    if any(item_class(i) is ErrorClass.ACCOUNT_PERMISSION for i in blocking):
        return "permission_decision"
    return None


def route_result(result: ListingResult, *, attempt: int) -> Decision:
    rows, items = _rows(result)
    ref, external = result.submission_ref, result.external_id
    if result.status == "published":
        return Decision(
            outcome="accepted",
            job_status="succeeded",
            listing_event="submission_accepted",
            listing_to="published",
            field_rows=rows,
            items=items,
            submission_ref=ref,
            external_id=external,
        )
    partial = result.status == "partially_published"
    default = ErrorCode.API_REJECTED_PARTIAL if partial else ErrorCode.PUBLISH_FAILED
    raw = result.error or ErrorReport(status="partial" if partial else "failed", error_code=default)
    report = stored_report(raw.model_copy(update={"error_code": raw.error_code or default}))
    blocking = report.blocking
    task: Task | None = None
    if attempt >= MAX_ATTEMPTS:
        event, to, reason = EXHAUSTED
    elif any(needs_action(i) for i in blocking):
        event, to, reason = ACTION
        task = _task(blocking)
    else:
        event, to = "submission_failed", "failed"
        reason = "publish_partial" if partial else "publish_failed"
    if task is None and any(i.error_code is UNKNOWN_CODE for i in blocking):
        task = "marketplace_error_review"
    return Decision(
        outcome="partially_accepted" if partial else "rejected",
        job_status="partial" if partial else "failed",
        error_code=report.error_code,
        report=report,
        listing_event=event,
        listing_to=to,
        reason=reason,
        open_task=task,
        field_rows=rows,
        items=items,
        submission_ref=ref,
        external_id=external,
    )


_ERROR_CODES: Final[Mapping[str, ErrorCode]] = {
    "rate_limited": ErrorCode.RATE_LIMITED,
    "auth_expired": ErrorCode.AUTH_EXPIRED,
    "permission_denied": ErrorCode.ACCOUNT_PERMISSION_DENIED,
}


def route_error(kind: ErrorKind, report: ErrorReport, *, attempt: int) -> Decision:
    if kind in ("ambiguous", "unavailable"):
        return Decision(outcome="pending", job_status="retry", report=report)
    code = _ERROR_CODES[kind]
    retryable = kind == "rate_limited"
    stored = report.model_copy(update={"error_code": code, "retryable": retryable})
    if attempt >= MAX_ATTEMPTS:
        event, to, reason = EXHAUSTED
        return Decision(
            outcome="error",
            job_status="failed",
            error_code=code,
            report=stored,
            listing_event=event,
            listing_to=to,
            reason=reason,
        )
    if kind == "rate_limited":
        return Decision(outcome="error", job_status="retry", error_code=code, report=stored)
    event, to, reason = ACTION
    return Decision(
        outcome="error",
        job_status="failed",
        error_code=code,
        report=stored,
        listing_event=event,
        listing_to=to,
        reason=reason,
        open_task="link_reconnect" if kind == "auth_expired" else "permission_decision",
    )
