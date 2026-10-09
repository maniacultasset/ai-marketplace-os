"""The ledger writes of one publish attempt, and the `listing_publish` terminal hook (S21 T14, T15;
DATABASE.md :871, :880, :887, :1330-1334, :1388, :1504-1532; CP2, CP8, CP10).

`apply_decision(uow, ctx, decision)` runs in the wrapper's one fenced terminal transaction (the
job's `persist`), so the submission, its field verdicts, the items, the issues, the listing event
and the tasks commit together with the job's final row, or not at all:

1. the `pending` row → the decision's outcome, fenced on this job and `pending` (0 rows: a newer
   job owns it, nothing else is written);
2. one `publish_field_results` row per (item, field) the result answered;
3. each item's state (`live` is never written here: only the status sync confirms it);
4. the adapter issues of the listing, closing only open ones on the fields this attempt sent
   (`sync_issues(..., scope_fields=sent)`, DATABASE.md:1330-1333);
5. the listing event with its columns in one UPDATE (`ck_published`): an accept sets
   `published_version_id`, the publish times, `marketplace_state = processing` ("Submitted"),
   `next_sync_at = now() + 2 min` and `sync_poll_count = 0`; a partial also schedules the sync
   (D7);
6. the account task (CP8): one `marketplace_error_review` per (account, kind), a
   `permission_decision`, or the issue linked to S15's `reconnect_account` task, so a later
   `resume_publish` can show `human_resolution`.

`revalidate(uow, ctx)` (CP10): facts or image bytes changed since the approval → the listing
leaves `publishing` for `needs_review` or `blocked/validation_errors` (event `revalidate`, the
approval voided) after the Rules Engine re-check; nothing was sent.

`on_publish_terminal(session, job, outcome)` (S13 `Hook`): it branches on the final
`job.status`, never on `outcome` (None on every path but success). A job that ended `failed`,
`dead_letter` or `cancelled` while its listing is still `publishing` and it is the listing's
newest publish job moves the listing to `failed/publish_failed` (a pending submission stays
`pending`: the next publish reads before it retries, CP1 (c)).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource, JobKind
from mkos_schemas.errors import ErrorItem, ErrorReport

from app.core.logging import get_logger
from app.db.models.ops import JobRun
from app.repositories.listings import DbNow, JobActor, ListingRepository
from app.repositories.submissions import NewFieldResult
from app.repositories.uow import TenantUnitOfWork, repositories_on
from app.services.issues import sync_issues
from app.services.publish_ledger import Decision
from app.workers.registry import Outcome

log = get_logger(__name__)

SOURCE: Final = "amazon_adapter"
FIRST_POLL: Final = timedelta(minutes=2)
ENDED: Final = frozenset({"failed", "dead_letter", "cancelled"})
AGGREGATE_ORDER: Final = ("processing", "pending_review", "rejected", "inactive")
_TASKS: Final[Mapping[str, tuple[str, str, str]]] = {
    "marketplace_error_review": (
        "account.error_review",
        "Review a marketplace error the system does not know yet.",
        "The marketplace refused a publish with an error that has no mapping; publishing waits "
        "for a person to look at it.",
    ),
    "permission_decision": (
        "account.category_approvals",
        "Grant the marketplace permission this listing needs.",
        "The marketplace refused a publish for an account or category permission.",
    ),
}


@dataclass(frozen=True, slots=True)
class AttemptCtx:
    """What the terminal write needs to know about the attempt."""

    job_id: uuid.UUID
    tenant_id: uuid.UUID
    listing_id: uuid.UUID
    version_id: uuid.UUID
    submission_id: uuid.UUID
    account_id: uuid.UUID
    seller_sku: str
    sent_paths: tuple[str, ...]
    raw_key: str | None = None
    raw_sha: bytes | None = None
    fence_job: bool = True
    """False for an ended job's `pending` row this job resolves (CP1 (c)): the write is fenced
    on the row still being `pending` only."""


def _aggregate(states: Sequence[str]) -> str:
    """DATABASE.md:756 over the non-parent items (live is never written here)."""
    if not states:
        return "processing"
    if all(s == "removed" for s in states):
        return "removed"
    for state in AGGREGATE_ORDER:
        if state in states:
            return state
    return "processing"


async def _items(
    uow: TenantUnitOfWork, ctx: AttemptCtx, decision: Decision
) -> tuple[dict[str, uuid.UUID], list[str]]:
    rows = {row.seller_sku: row for row in await uow.listing_items.for_listing(ctx.listing_id)}
    for item in decision.items:
        row = rows.get(item.seller_sku)
        if row is None:
            continue
        values: dict[str, Any] = {}
        if item.state.value != row.marketplace_state:
            values["marketplace_state"] = item.state.value
        if item.external_id and row.external_id is None:
            values["external_id"] = item.external_id[:128]
        if values:
            await uow.listing_items.write_state(row.id, **values)
            if "marketplace_state" in values:
                row.marketplace_state = values["marketplace_state"]
    states = [r.marketplace_state for r in rows.values() if r.item_role != "parent"]
    return {sku: r.id for sku, r in rows.items()}, states


async def _field_rows(
    uow: TenantUnitOfWork, ctx: AttemptCtx, decision: Decision, item_ids: Mapping[str, uuid.UUID]
) -> None:
    rows = [
        NewFieldResult(
            submission_id=ctx.submission_id,
            listing_id=ctx.listing_id,
            listing_version_id=ctx.version_id,
            listing_item_id=item_ids[r.seller_sku],
            field=r.field,
            status=r.status,
            error_json=None if r.error is None else r.error.model_dump(mode="json"),
            marketplace_code=None if r.error is None else r.error.raw_code,
        )
        for r in decision.field_rows
        if r.seller_sku in item_ids and (r.status != "failed" or r.error is not None)
    ]
    await uow.field_results.insert_many(rows)


async def _open_task(
    uow: TenantUnitOfWork, ctx: AttemptCtx, listing: Any, decision: Decision
) -> None:
    kind = decision.open_task
    if kind is None:
        return
    if kind == "link_reconnect":
        await uow.human_tasks.open_reconnect(ctx.account_id)
        task_id = await uow.human_tasks.live_account_task(
            ctx.account_id, "reconnect_account", "account.oauth"
        )
    else:
        target, question, reason = _TASKS[kind]
        if kind == "permission_decision":
            target = f"{target}.{listing.category}"[:120]
        task_id = await uow.human_tasks.open_account_task(
            kind, ctx.account_id, target, question, reason, {"kind": "acknowledge"}
        )
    if task_id is not None:
        await uow.listing_issues.link_task(ctx.listing_id, ctx.job_id, task_id)


def _issue_report(decision: Decision) -> ErrorReport:
    if decision.report is not None:
        return decision.report
    return ErrorReport(status="passed")


async def apply_decision(uow: TenantUnitOfWork, ctx: AttemptCtx, decision: Decision) -> bool:
    """The terminal ledger write (module docstring). False when the row was no longer this
    job's `pending` row (a stale job): nothing is written."""
    report = decision.report
    resolved = await uow.submissions.resolve(
        ctx.submission_id,
        outcome=decision.outcome,
        job_id=ctx.job_id if ctx.fence_job else None,
        error_code=None if decision.error_code is None else decision.error_code.value,
        report=None if report is None else report.model_dump(mode="json"),
        submission_ref=decision.submission_ref,
        raw_key=ctx.raw_key,
        raw_sha=ctx.raw_sha,
    )
    if not resolved:
        log.warning("publish.stale_resolve_dropped", job_run_id=str(ctx.job_id))
        return False
    listing = await uow.listings.get_for_update(ctx.listing_id)
    if listing is None:
        return False
    item_ids, states = await _items(uow, ctx, decision)
    await _field_rows(uow, ctx, decision, item_ids)
    await sync_issues(
        uow,
        listing,
        SOURCE,
        _issue_report(decision),
        run_id=None,
        cause="publish",
        scope_fields=list(ctx.sent_paths),
        version_id=ctx.version_id,
        job_id=ctx.job_id,
    )
    await _move(uow, ctx, listing, decision, states)
    await _open_task(uow, ctx, listing, decision)
    log.info(
        "publish.resolved",
        job_run_id=str(ctx.job_id),
        listing_id=str(ctx.listing_id),
        outcome=decision.outcome,
        code=None if decision.error_code is None else decision.error_code.value,
    )
    return True


async def _move(
    uow: TenantUnitOfWork,
    ctx: AttemptCtx,
    listing: Any,
    decision: Decision,
    states: Sequence[str],
) -> None:
    if decision.listing_event is None or decision.listing_to is None:
        return
    if listing.status != "publishing":
        return
    columns: dict[str, Any] = {}
    aggregate = _aggregate(states)
    if decision.outcome == "accepted":
        columns = {
            "published_version_id": ctx.version_id,
            "last_published_at": DbNow(),
            "marketplace_state": aggregate,
            "next_sync_at": DbNow(FIRST_POLL),
            "sync_poll_count": 0,
        }
        if listing.first_published_at is None:
            columns["first_published_at"] = DbNow()
        if listing.marketplace_sku is None:
            columns["marketplace_sku"] = ctx.seller_sku
        if decision.external_id and listing.external_id is None:
            columns["external_id"] = decision.external_id[:128]
    elif decision.outcome == "partially_accepted":
        # D7: the accepted items are synced too; the listing keeps its failed status
        columns = {
            "marketplace_state": aggregate,
            "next_sync_at": DbNow(FIRST_POLL),
            "sync_poll_count": 0,
        }
    if columns.get("marketplace_state") not in (None, listing.marketplace_state):
        columns["marketplace_state_changed_at"] = DbNow()
    await uow.listings.move(
        listing,
        to=decision.listing_to,
        event=decision.listing_event,
        reason=decision.reason,
        actor=JobActor(ctx.job_id),
        columns=columns,
    )


async def move_only(uow: TenantUnitOfWork, ctx: AttemptCtx, decision: Decision) -> None:
    """Only the listing event (the attempt's row was resolved earlier, e.g. a 3rd attempt the
    marketplace never applied)."""
    listing = await uow.listings.get_for_update(ctx.listing_id)
    if listing is None:
        return
    await _move(uow, ctx, listing, decision, [])


async def revalidate(uow: TenantUnitOfWork, job_id: uuid.UUID, listing_id: uuid.UUID) -> str | None:
    """CP10: the Rules Engine re-check; the listing moves out of `publishing` with the approval
    voided (`needs_review`, or `blocked/validation_errors` on a blocking finding)."""
    from app.services.adapter_input import canonical_listing
    from app.services.listings import human_move, rules_check

    listing = await uow.listings.get_for_update(listing_id)
    if listing is None or listing.status != "publishing":
        return None
    source = await uow.listings.preview_source(listing_id)
    to, reason = "needs_review", None
    if source is not None and source.version is not None:
        check = await rules_check(uow, listing, canonical_listing(source))
        to, reason = human_move(check.report)
        if to == "validated":  # publishing has no edge to validated: a person looks again
            to, reason = "needs_review", None
    await uow.listings.move(
        listing,
        to=to,
        event="revalidate",
        reason=reason,
        actor=JobActor(job_id),
        clear_approval=True,
    )
    log.info("publish.revalidated", job_run_id=str(job_id), listing_id=str(listing_id), to=to)
    return to


def refusal_report(code: ErrorCode, field: str, message: str, attempt: int = 1) -> ErrorReport:
    """A one-item report of a publish the job refused before sending anything."""
    return ErrorReport(
        status="failed",
        error_code=code,
        retryable=False,
        attempts_used=min(max(attempt, 0), 3),
        errors=[
            ErrorItem(
                field=field,
                error_code=code,
                message=message,
                source=ErrorSource.SYSTEM,
                retryable=False,
            )
        ],
    )


# ---- the terminal hook (T15) ----


async def on_publish_terminal(session: Any, job: JobRun, outcome: Outcome | None) -> None:
    del outcome  # S13 `Hook`: branch on the final status only
    if job.status not in ENDED or job.listing_id is None:
        return
    listings = ListingRepository(session, job.tenant_id)
    listing = await listings.get_for_update(job.listing_id)
    if listing is None or listing.status != "publishing":
        return
    if await listings.latest_job_id(listing.id, JobKind.LISTING_PUBLISH) != job.id:
        return  # a newer publish job owns the listing
    await listings.move(
        listing,
        to="failed",
        event="submission_failed",
        reason="publish_failed",
        actor=JobActor(job.id),
    )
    log.info("publish.listing_failed", job_run_id=str(job.id), job_status=job.status)


async def on_publish_requeue(session: Any, old: JobRun, new: JobRun) -> None:
    """A dead-lettered publish is never requeued by staff: the user publishes again, and the
    new job reads the marketplace before it retries (CP1 (c), CP3)."""
    from app.repositories.jobs import NotEligibleError

    del session, new
    log.info("publish.requeue_refused", job_run_id=str(old.id))
    raise NotEligibleError("publish again through the API")


def uow_on(session: Any, tenant_id: uuid.UUID) -> TenantUnitOfWork:
    return repositories_on(session, tenant_id)
