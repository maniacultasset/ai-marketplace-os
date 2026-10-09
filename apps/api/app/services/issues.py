"""The issue inbox (S19 T18-T20; DATABASE.md §5.7 :1304-1336; CP-D2).

`sync_issues(uow, listing, source, report, *, run_id, cause, scope_fields=None, ...)` runs in the
transaction that writes a version-linked `validation_runs` row (S18's backend re-check, the PATCH
re-check, `validate`) or, from S21, an adapter result (`run_id=None`, `scope_fields` = the paths
sent):

1. each `ErrorItem` keyed `source|field|error_code|COALESCE(rule_id, raw_code, 'template:'+key, '')`
   (`key` is the template attribute of a `rules_engine` item with no rule, `attributes.<key>`);
   class, retry class and fix action from the item, else `ERROR_CODE_META` (else `unknown`);
2. upserted on the open-issue index (a repeat counts an occurrence; a fresh row links the
   latest closed row with the same key as its `recurrence_of_id`);
3. the open rows of this listing and source whose key the report no longer has are closed, with
   the resolution the cause names: `edit` → `human_edit`, `regenerate` → `regenerated`, anything
   else → `revalidated_clean`. An adapter result closes only rows on the fields it sent.

The per-attempt rows the generation graph writes (no version) never reach the inbox.
Step 4 (linking `user_action` items to human tasks) is C2, after v1.

`list_issues` and `dismiss` serve the two routes. A dismissal accepts an open **warning**; an
error is never dismissed (the Rules Engine stays the arbiter, invariant 2): 409 before any SQL,
with `listing_issues_guard` (GRD01) as the backstop.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from mkos_schemas.codes import ERROR_CODE_META, ErrorCode
from mkos_schemas.errors import ErrorItem, ErrorReport

from app.core.errors import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.core.rbac import Principal
from app.repositories.listing_issues import IssueFilter, NewIssue
from app.repositories.uow import TenantUnitOfWork, repositories_on, tenant_uow

log = get_logger(__name__)

ISSUE_SOURCES: Final = frozenset(
    {"schema", "rules_engine", "vision_model", "amazon_adapter", "flipkart_adapter"}
)
RESOLUTIONS: Final = {"edit": "human_edit", "regenerate": "regenerated"}
VALUE_MAX: Final = 600
TEMPLATE_PREFIX: Final = "attributes."
Keyset = tuple[datetime, uuid.UUID]


@dataclass(frozen=True, slots=True)
class SyncResult:
    opened: int
    repeated: int
    closed: int


def resolution_for(cause: str) -> str:
    return RESOLUTIONS.get(cause, "revalidated_clean")


def dedupe_key(source: str, item: ErrorItem, listing_item_id: uuid.UUID | None = None) -> str:
    """DATABASE.md:1331, exactly; `|item:<id>` when the item names a SKU."""
    term = ""
    if item.rule_id is not None:
        term = str(item.rule_id)
    elif item.raw_code:
        term = item.raw_code
    elif source == "rules_engine" and item.field.startswith(TEMPLATE_PREFIX):
        term = "template:" + item.field.removeprefix(TEMPLATE_PREFIX)
    key = f"{source}|{item.field}|{item.error_code.value}|{term}"
    return key if listing_item_id is None else f"{key}|item:{listing_item_id}"


def classify(item: ErrorItem) -> tuple[str, str, str | None]:
    """`(error_class, retry_class, fix_action)`: the item's, else the code's metadata."""
    meta = ERROR_CODE_META.get(item.error_code)
    error_class = item.error_class or (meta.error_class if meta else None)
    retry_class = item.retry_class or (meta.retry_class if meta else None)
    fix_action = item.fix_action or (meta.fix_action if meta else None)
    return (
        "unknown" if error_class is None else error_class.value,
        "user_action" if retry_class is None else retry_class.value,
        None if fix_action is None else fix_action.value,
    )


def _issue(
    listing: Any,
    source: str,
    item: ErrorItem,
    key: str,
    *,
    run_id: uuid.UUID | None,
    version_id: uuid.UUID | None,
    job_id: uuid.UUID | None,
) -> NewIssue:
    error_class, retry_class, fix_action = classify(item)
    value = item.value_submitted
    return NewIssue(
        listing_id=listing.id,
        marketplace=listing.marketplace,
        source=source,
        field_path=item.field,
        error_code=item.error_code.value,
        error_class=error_class,
        retry_class=retry_class,
        severity=item.severity.value,
        message=item.message,
        dedupe_key=key,
        listing_version_id=version_id,
        validation_run_id=run_id,
        job_id=job_id,
        auto_fixable=fix_action is not None,
        fix_action=fix_action,
        rule_id=item.rule_id,
        marketplace_code=item.raw_code,
        suggestion=item.suggestion,
        value_submitted=None if value is None else value[:VALUE_MAX],
    )


async def sync_issues(
    uow: TenantUnitOfWork,
    listing: Any,
    source: str,
    report: ErrorReport,
    *,
    run_id: uuid.UUID | None,
    cause: str,
    scope_fields: Sequence[str] | None = None,
    version_id: uuid.UUID | None = None,
    job_id: uuid.UUID | None = None,
) -> SyncResult:
    if source not in ISSUE_SOURCES:
        raise ValueError(f"{source} is not an issue source")
    items: dict[str, ErrorItem] = {}
    for item in report.errors:
        if scope_fields is not None and item.field not in scope_fields:
            continue
        items.setdefault(dedupe_key(source, item), item)
    opened = repeated = 0
    for key, item in items.items():
        new = _issue(
            listing, source, item, key, run_id=run_id, version_id=version_id, job_id=job_id
        )
        _row, created = await uow.listing_issues.upsert_open(new)
        if created:
            opened += 1
        else:
            repeated += 1
    closed = await uow.listing_issues.close_missing(
        listing.id,
        source,
        list(items),
        resolution_for(cause),
        field_paths=scope_fields,
    )
    result = SyncResult(opened=opened, repeated=repeated, closed=closed)
    if opened or closed:
        log.info(
            "issues.synced",
            listing_id=str(listing.id),
            source=source,
            opened=opened,
            repeated=repeated,
            closed=closed,
        )
    return result


# ---- the inbox routes ----


def issue_fields(row: Any) -> dict[str, Any]:
    """The `IssueOut` fields of a row (the internal keys stay out)."""
    names = (
        "id", "listing_id", "marketplace", "listing_item_id", "listing_version_id", "source",
        "field_path", "error_code", "error_class", "retry_class", "severity", "auto_fixable",
        "fix_action", "marketplace_code", "raw_message", "message", "plain_message", "suggestion",
        "value_submitted", "occurrences", "first_seen_at", "last_seen_at", "status",
        "resolution", "action_taken", "resolved_at", "resolved_by_user_id", "human_task_id",
        "recurrence_of_id", "created_at", "updated_at",
    )  # fmt: skip
    return {name: getattr(row, name) for name in names}


async def list_issues(
    principal: Principal, filters: IssueFilter, *, after: Keyset | None, limit: int
) -> tuple[list[dict[str, Any]], bool, Keyset | None]:
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        rows, more = await uow.listing_issues.page(filters, after=after, limit=limit)
    next_key = (rows[-1].created_at, rows[-1].id) if more and rows else None
    return [issue_fields(r) for r in rows], more, next_key


async def dismiss_issue(
    uow: TenantUnitOfWork, principal: Principal, issue_id: uuid.UUID
) -> dict[str, Any]:
    """Lock, 404, the 409s before SQL, the compare-and-set, the audit."""
    issue = await uow.listing_issues.lock(issue_id)
    if issue is None:
        raise NotFoundError("issue_id")
    if issue.status != "open" or issue.severity != "warning":
        raise ConflictError(ErrorCode.INVALID_STATUS_TRANSITION, "listing_issues")
    done = await uow.listing_issues.dismiss(issue.id, principal.user_id)
    if done is None:  # unreachable under the row lock
        raise ConflictError(ErrorCode.INVALID_STATUS_TRANSITION, "listing_issues")
    await uow.audit.write(
        tenant_id=principal.tenant_id,
        actor_type="user",
        actor_user_id=principal.user_id,
        action="listing_issue.dismissed",
        entity_type="listing_issue",
        entity_id=done.id,
        before=json.dumps({"status": "open"}),
        after=json.dumps({"status": done.status, "resolution": done.resolution}),
    )
    log.info("listing_issue.dismissed", issue_id=str(done.id))
    return issue_fields(done)


async def accept_dismiss(
    principal: Principal, key: str, path: str, issue_id: uuid.UUID
) -> dict[str, Any]:
    """The route's idempotent transaction: 200 with the issue, stored under the key."""
    from app.services.idempotency import idempotent

    async def handler(session: Any) -> tuple[int, dict[str, Any]]:
        uow = repositories_on(session, principal.tenant_id)
        fields = await dismiss_issue(uow, principal, issue_id)
        return 200, _json(fields)

    _status, stored = await idempotent(principal, key, "POST", path, b"", handler)
    return stored


def _json(fields: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, value in fields.items():
        if isinstance(value, uuid.UUID):
            out[name] = str(value)
        elif isinstance(value, datetime):
            out[name] = value.isoformat()
        else:
            out[name] = value
    return out
