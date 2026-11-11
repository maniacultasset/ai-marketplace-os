"""The resolution read (SI11 T20; ADR-0028 proposed; P:B8; CP-D18): `GET /api/v1/listings/{id}/
resolution`, what Kuinn checked, changed and still needs for one listing, and the lane and cause
of each issue row the inbox shows.

- `labels(rows, ...)` (pure): an issue's lane and cause. A row a repair was written for, or one
  closed by a repair, is `auto_repair`; a row Kuinn's content fix closed (`deterministic_fix`) is
  `auto_content` and one its AI rewrite closed (`ai_correction`) `ai_field`; a row linked to a
  task takes the task's lane (a `permission_decision` is the marketplace's, a
  `marketplace_error_review` staff's, a fact or image question the seller's); an open row a live
  or succeeded fix job covers (`fix_lanes`: its payload's `issue_ids`, by `mode`) takes that
  job's lane; any other row the planner's own rules (`plan.label`). WARNINGs carry no lane. The
  read never re-runs the engine's content or AI checks: the lane is what Kuinn did.
- `state_of(facts)` (pure): the precedence `live` > `fixing` > `checking` > `resubmitted` >
  `stopped` > `needs_marketplace` > `needs_you` > `fix_ready` > `idle`. A generation that failed
  (`failed/generation_failed`) with blocking rows open is `stopped` too, after `needs_you` (its
  next step `regenerate`), so a fix job that ended with no version never reads `idle`.
- `attempts(...)` (pure): one `precheck` per dry-run job and one per submission, newest first,
  at most 10. An attempt's window runs from its start to the next attempt's start: its failures
  are the ERROR rows first seen in it (the dry run's channel and version for a pre-check, the
  live channel for a submission), its changes the repairs written, the rows Kuinn closed and the
  seller answers given in it.
- `resolution_for(principal, listing_id)`: the read; another tenant's listing (or none) is 404.

The read never runs the engine and never writes.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from mkos_schemas.codes import ErrorCode

from app.core.errors import NotFoundError
from app.core.rbac import Principal
from app.repositories.resolution import FIX_JOB_STATUSES, LinkedTask, VersionRow
from app.repositories.uow import TenantUnitOfWork, tenant_uow
from app.services.resolution.plan import (
    ADAPTER_SOURCES,
    Cause,
    IssueView,
    Lane,
    PlanContext,
    cause_of,
    label,
)

MAX_CYCLES: Final = 3
MAX_ATTEMPTS: Final = 10
MAX_ITEMS: Final = 50
LIVE_TASK: Final = frozenset({"open", "answered"})
LIVE_JOB: Final = frozenset({"queued", "running"})
SELLER_TASKS: Final = frozenset({"missing_fact", "conflicting_facts", "image_replacement"})
FIX_MODE_LANES: Final[Mapping[str, Lane]] = {"auto_fix": "auto_content", "ai_field": "ai_field"}
CLOSED_LANES: Final[Mapping[str, Lane]] = {
    "deterministic_fix": "auto_content",
    "ai_correction": "ai_field",
}
TASK_LANES: Final[Mapping[str, Lane]] = {
    "permission_decision": "marketplace",
    "marketplace_error_review": "staff",
    **dict.fromkeys(SELLER_TASKS, "seller"),
}
PRECHECK_VERDICTS: Final = frozenset({"clean", "issues", "failed"})
OPEN_SUBMISSION: Final = frozenset({"pending", "accepted", "partially_accepted"})
PREVIEW_SUFFIX: Final = "|preview"
# SI13 (CP-D8): the seller actions that open no inbox question, by the issue's code
ACTION_OF: Final[Mapping[str, str]] = {
    "CATEGORY_MISMATCH": "recategorize",
    "IDENTIFIER_DUPLICATE": "edit_identifier",
    "IDENTIFIER_INVALID": "edit_identifier",
    "IDENTIFIER_MISSING": "edit_identifier",
}
CONTENT_ROOTS: Final = ("content.", "keywords.")
SELLER_ACTION_TEXT: Final[Mapping[str, str]] = {
    "recategorize": "Amazon files this product under another category. Choose the matching "
    "category, then regenerate the listing.",
    "edit_identifier": "Amazon does not accept this product's barcode or GTIN exemption. Check the "
    "barcode on the product, or choose the exemption only if Amazon approved it for this brand "
    "and category.",
    "edit_content": "This text does not pass the marketplace's rules, for example a title that "
    "is too long. Edit the listing to fix it, then save a new version.",
}


# ---- labels ----


def _view(row: Any) -> IssueView:
    return IssueView(
        id=row.id,
        source=row.source,
        field_path=row.field_path,
        error_code=row.error_code,
        error_class=row.error_class,
        severity=row.severity,
        marketplace_code=row.marketplace_code,
        channel="preview" if row.dedupe_key.endswith(PREVIEW_SUFFIX) else "live",
    )


def fix_lanes(jobs: Iterable[Any]) -> dict[uuid.UUID, Lane]:
    """issue id -> the lane of the live or succeeded resolution fix job that covers it (the
    newest job wins: `jobs` come newest first)."""
    out: dict[uuid.UUID, Lane] = {}
    for job in jobs:
        payload = job.payload_json or {}
        if (
            job.kind != "listing_generate"
            or payload.get("trigger") != "resolution"
            or job.status not in FIX_JOB_STATUSES
        ):
            continue
        lane = FIX_MODE_LANES.get(str(payload.get("mode", "")))
        if lane is None:
            continue
        for raw in payload.get("issue_ids") or ():
            try:
                out.setdefault(uuid.UUID(str(raw)), lane)
            except ValueError:
                continue
    return out


def labels(
    rows: Iterable[Any],
    *,
    repaired: Iterable[uuid.UUID] = (),
    task_kinds: Mapping[uuid.UUID, str] | None = None,
    critical_keys: frozenset[str] = frozenset(),
    fixing: Mapping[uuid.UUID, Lane] | None = None,
    recategorizable: Iterable[uuid.UUID] = (),
) -> dict[uuid.UUID, tuple[Lane, Cause]]:
    """issue id -> (lane, cause); WARNING rows are left out. `fixing` is `fix_lanes(jobs)`;
    `recategorizable` the listings the seller may still recategorise (SI13, CP-D7)."""
    repaired_ids = set(repaired)
    kinds = task_kinds or {}
    covered = fixing or {}
    ctx = PlanContext(critical_keys=critical_keys)
    movable = set(recategorizable)
    ctx_movable = PlanContext(critical_keys=critical_keys, recategorizable=True)
    out: dict[uuid.UUID, tuple[Lane, Cause]] = {}
    for row in rows:
        if row.severity != "error":
            continue
        view = _view(row)
        lane: Lane | None = None
        if row.id in repaired_ids or (row.action_taken or "").startswith("repair:"):
            lane = "auto_repair"
        elif row.status != "open" and row.resolution in CLOSED_LANES:
            lane = CLOSED_LANES[row.resolution]
        elif row.human_task_id is not None and kinds.get(row.human_task_id) in TASK_LANES:
            lane = TASK_LANES[kinds[row.human_task_id]]
        elif row.status == "open" and row.id in covered:
            lane = covered[row.id]
        if lane is not None:
            out[row.id] = (lane, cause_of(view, lane, {}))
            continue
        found = label(view, ctx_movable if getattr(row, "listing_id", None) in movable else ctx)
        if found is not None:
            out[row.id] = found
    return out


async def recategorizable_listings(
    uow: TenantUnitOfWork, rows: Sequence[Any]
) -> frozenset[uuid.UUID]:
    """SI13: the listings of the open adapter CATEGORY_MISMATCH rows that the seller may still
    recategorise (the planner's rule 2 context, read only)."""
    from app.services.product_recategorize import recategorizable

    ids = sorted(
        {
            r.listing_id
            for r in rows
            if r.status == "open" and r.error_code == ErrorCode.CATEGORY_MISMATCH.value
        }
    )
    found: set[uuid.UUID] = set()
    for listing_id in ids[:MAX_ITEMS]:
        listing = await uow.listings.get(listing_id)
        if listing is not None and await recategorizable(uow, listing):
            found.add(listing_id)
    return frozenset(found)


def seller_action(rows: Iterable[Any], lanes: Mapping[uuid.UUID, tuple[Lane, Cause]]) -> str | None:
    """SI13 (CP-D8): the seller action an open ERROR asks for with no inbox question:
    `recategorize` (a category the marketplace refiled) before `edit_identifier` (a barcode
    Amazon refused, or no barcode and no approved GTIN exemption) before `edit_content` (text
    that failed Kuinn's own check)."""
    kinds: set[str] = set()
    for r in rows:
        if r.status != "open" or r.severity != "error" or lanes.get(r.id, ("",))[0] != "seller":
            continue
        if r.error_code in ACTION_OF:
            kinds.add(ACTION_OF[r.error_code])
        elif r.source not in ADAPTER_SOURCES and r.field_path.startswith(CONTENT_ROOTS):
            kinds.add("edit_content")  # QA: the seller's own text failed Kuinn's check
    for kind in ("recategorize", "edit_identifier", "edit_content"):
        if kind in kinds:
            return kind
    return None


async def labels_for(
    uow: TenantUnitOfWork, rows: Sequence[Any]
) -> dict[uuid.UUID, tuple[Lane, Cause]]:
    """`labels` with the repairs, the linked tasks and the critical keys read from the store."""
    errors = [r for r in rows if r.severity == "error"]
    if not errors:
        return {}
    repaired = await uow.resolution.repaired_issue_ids([r.id for r in errors])
    tasks = await uow.resolution.task_kinds(
        sorted({r.human_task_id for r in errors if r.human_task_id is not None})
    )
    jobs = await uow.resolution.fix_jobs(
        sorted({r.listing_id for r in errors if r.status == "open"})
    )
    return labels(
        errors,
        repaired=repaired,
        task_kinds={k: v[0] for k, v in tasks.items()},
        critical_keys=await uow.product_facts.critical_keys(),
        fixing=fix_lanes(jobs),
        recategorizable=await recategorizable_listings(uow, errors),
    )


# ---- state ----


@dataclass(frozen=True, slots=True)
class ReadFacts:
    status: str
    status_reason: str | None = None
    remote_check_status: str | None = None
    live_preview: bool = False
    live_status_sync: bool = False
    """A `listing_status_sync` job of any op is live: only then is a `pending` stamp a wait (a
    stamp left behind with no job, fix round 2, is not)."""
    live_fix_job: bool = False
    latest_submission: tuple[str, str] | None = None
    """(actor_type, outcome) of the newest submission."""
    cycles: int = 0
    open_blocking: bool = False
    marketplace_tasks: tuple[uuid.UUID, ...] = ()
    seller_tasks: tuple[uuid.UUID, ...] = ()
    fix_ready: bool = False
    """The current version came from an automatic fix job and is not approved."""
    waiting_repairs: tuple[str, ...] = ()
    """The fields of open `auto_repair` rows (a published listing's repairs wait, CP-D21)."""
    seller_action: str | None = None
    """SI13 (CP-D8): `recategorize` or `edit_identifier` when an open seller-lane ERROR asks
    for that action on the listing (no inbox question)."""
    answer_ready: bool = False
    """The current version is a seller's own fact answer or edit made after a refused or failed
    send, checked and not approved: a teammate approves and resends it."""


def state_of(f: ReadFacts) -> str:
    if f.status == "published":
        return "live"
    if f.live_fix_job:
        return "fixing"
    if f.live_preview or (f.remote_check_status == "pending" and f.live_status_sync):
        return "checking"
    if (
        f.latest_submission is not None
        and f.latest_submission[0] == "system"
        and f.latest_submission[1] in OPEN_SUBMISSION
    ):
        return "resubmitted"
    if f.status_reason == "corrections_exhausted" or (f.cycles >= MAX_CYCLES and f.open_blocking):
        return "stopped"
    if (
        f.status == "blocked"
        and f.status_reason == "marketplace_action_required"
        and f.marketplace_tasks
    ):
        return "needs_marketplace"
    if f.seller_tasks or f.seller_action:
        return "needs_you"
    if f.status == "blocked" and f.status_reason == "human_task_open":
        return "needs_you"  # every question answered: only a regenerate uses the answers
    if f.status == "failed" and f.status_reason == "generation_failed" and f.open_blocking:
        return "stopped"  # a fix job ended with no version: never `idle` with nobody told
    if f.fix_ready or f.answer_ready:
        return "fix_ready"
    return "idle"


def next_step(state: str, f: ReadFacts) -> dict[str, Any]:
    if state == "live":
        if f.waiting_repairs:
            fields = ", ".join(sorted(set(f.waiting_repairs)))[:200]
            return {
                "kind": "none",
                "text": f"Kuinn's fix for {fields} goes out with the next update.",
            }
        return {"kind": "none"}
    if state == "fixing":
        return {"kind": "wait", "text": "Kuinn is preparing a fix for the listing."}
    if state == "checking":
        return {"kind": "wait", "text": "Kuinn is checking the listing with the marketplace."}
    if state == "resubmitted":
        return {
            "kind": "wait",
            "text": "Kuinn sent the fixed listing again; waiting for the marketplace.",
        }
    if state == "stopped" and f.status_reason == "generation_failed":
        return {
            "kind": "regenerate",
            "task_ids": list(f.marketplace_tasks)[:20],
            "text": "Kuinn could not fix the listing. Regenerate it or edit the field to try again.",
        }
    if state == "stopped":
        return {
            "kind": "resume",
            "task_ids": list(f.seller_tasks + f.marketplace_tasks)[:20],
            "text": "Kuinn tried 3 times. Edit the listing or answer the open question to try again.",
        }
    if state == "needs_marketplace":
        return {
            "kind": "marketplace",
            "task_ids": list(f.marketplace_tasks)[:20],
            "text": "The marketplace must approve this brand or category first (see the task).",
        }
    if state == "needs_you" and f.seller_action is not None and not f.seller_tasks:
        return {"kind": f.seller_action, "text": SELLER_ACTION_TEXT[f.seller_action]}
    if state == "needs_you" and not f.seller_tasks and f.status_reason == "human_task_open":
        return {
            "kind": "regenerate",
            "text": "Your answers are saved. Generate the listing again to use them.",
        }
    if state == "needs_you":
        return {
            "kind": "answer",
            "task_ids": list(f.seller_tasks)[:20],
            "text": "Answer the open question so Kuinn can fix the listing.",
        }
    if state == "fix_ready" and not f.fix_ready:
        return {
            "kind": "approve",
            "text": "Your change is in a new version. A teammate who did not make it approves "
            "and resends it.",
        }
    if state == "fix_ready":
        return {
            "kind": "approve",
            "text": "Kuinn prepared a fix. Another team member approves and resends it.",
        }
    return {"kind": "none"}


# ---- attempts ----


@dataclass(frozen=True, slots=True)
class _Attempt:
    kind: str
    start: datetime
    at: datetime
    number: int
    version_id: uuid.UUID | None
    actor: str
    outcome: str
    out: dict[str, Any] = field(default_factory=dict)


def _precheck(job: Any) -> _Attempt | None:
    payload = job.payload_json or {}
    if job.kind != "listing_status_sync" or payload.get("op") != "preview":
        return None
    verdict = (job.result_json or {}).get("remote_check")
    if verdict in PRECHECK_VERDICTS:
        outcome = str(verdict)
    else:
        outcome = "pending" if job.status in LIVE_JOB else "failed"
    raw = payload.get("version_id")
    try:
        version_id = uuid.UUID(str(raw)) if raw else None
    except ValueError:
        version_id = None
    return _Attempt(
        "precheck",
        start=job.queued_at,
        at=job.finished_at or job.queued_at,
        number=0,
        version_id=version_id,
        actor="system",
        outcome=outcome,
    )


def _submission(row: Any, number: int) -> _Attempt:
    return _Attempt(
        "submission",
        start=row.created_at,
        at=row.created_at,
        number=number,
        version_id=row.listing_version_id,
        actor="system" if row.actor_type == "system" else "user",
        outcome=row.outcome,
    )


def _numbered(submissions: Sequence[Any], total: int | None) -> list[_Attempt]:
    """Each send numbered by its order on the whole listing (QA: `attempt` restarts at 1 for
    every version, so a resend of version 2 read "Sent 1" again). `submissions` are the newest
    ones, newest first; `total` is the listing's count (None: the page is all of them)."""
    count = len(submissions) if total is None else max(total, len(submissions))
    return [_submission(row, min(count - index, 99)) for index, row in enumerate(submissions)]


def _within(at: datetime | None, start: datetime, end: datetime | None) -> bool:
    return at is not None and at >= start and (end is None or at < end)


def _text(value: str, limit: int) -> str:
    return value[:limit] if value else "-"


def attempts(
    *,
    jobs: Sequence[Any],
    submissions: Sequence[Any],
    issues: Sequence[Any],
    repairs: Sequence[Any],
    tasks: Sequence[LinkedTask],
    versions: Mapping[uuid.UUID, VersionRow],
    lanes: Mapping[uuid.UUID, tuple[Lane, Cause]],
    total_submissions: int | None = None,
) -> list[dict[str, Any]]:
    found = [a for a in (_precheck(j) for j in jobs) if a is not None]
    found += _numbered(submissions, total_submissions)
    found = [a for a in found if a.version_id in versions]
    found.sort(key=lambda a: (a.start, a.kind))
    by_issue = {i.id: i for i in issues}
    out: list[dict[str, Any]] = []
    for index, attempt in enumerate(found):
        end = found[index + 1].start if index + 1 < len(found) else None
        preview = attempt.kind == "precheck"
        failed = []
        for row in issues:
            if row.severity != "error" or not _within(row.first_seen_at, attempt.start, end):
                continue
            if row.dedupe_key.endswith(PREVIEW_SUFFIX) != preview:
                continue
            if preview and row.listing_version_id != attempt.version_id:
                continue
            lane, cause = lanes.get(row.id, ("staff", "unknown"))
            failed.append(
                {
                    "field": _text(row.field_path, 192),
                    "message": _text(row.plain_message or row.message, 1000),
                    "lane": lane,
                    "cause": cause,
                    "marketplace_code": (row.marketplace_code or None)
                    and row.marketplace_code[:64],
                }
            )
        changed: list[dict[str, Any]] = []
        for repair in sorted(repairs, key=lambda r: r.created_at):
            if _within(repair.created_at, attempt.start, end):
                changed.append(
                    {
                        "kind": "repair",
                        "field": _text(repair.field_path, 192),
                        "rule": repair.rule,
                        "summary": _text(
                            f"Kuinn set {repair.attribute} the way the marketplace accepts it.",
                            300,
                        ),
                    }
                )
        for row in issues:
            if row.resolution not in ("deterministic_fix", "ai_correction") or (
                row.action_taken or ""
            ).startswith("repair:"):
                continue
            if not _within(row.resolved_at, attempt.start, end):
                continue
            kind = "ai_correction" if row.resolution == "ai_correction" else "content_fix"
            changed.append(
                {
                    "kind": kind,
                    "field": _text(row.field_path, 192),
                    "summary": _text(
                        row.action_taken
                        or (
                            "Kuinn rewrote this field."
                            if kind == "ai_correction"
                            else "Kuinn fixed this field."
                        ),
                        300,
                    ),
                }
            )
        seen: set[uuid.UUID] = set()
        for task in tasks:
            if task.kind not in SELLER_TASKS or task.task_id in seen:
                continue
            if task.status not in ("answered", "applied") or not _within(
                task.answered_at, attempt.start, end
            ):
                continue
            seen.add(task.task_id)
            issue = by_issue.get(task.issue_id)
            path = task.target_path or (issue.field_path if issue is not None else "listing")
            changed.append(
                {
                    "kind": "seller_answer",
                    "field": _text(path, 192),
                    "summary": "The seller answered this question.",
                }
            )
        version = versions[attempt.version_id]  # type: ignore[index]
        out.append(
            {
                "kind": attempt.kind,
                "number": attempt.number,
                "version": version.version,
                "at": attempt.at,
                "actor": attempt.actor,
                "outcome": attempt.outcome,
                "failed": failed[:MAX_ITEMS],
                "changed": changed[:MAX_ITEMS],
            }
        )
    out.reverse()
    return out[:MAX_ATTEMPTS]


# ---- the read ----


def _is_fix_job(job: Any) -> bool:
    return (
        job.kind == "listing_generate" and (job.payload_json or {}).get("trigger") == "resolution"
    )


async def build(uow: TenantUnitOfWork, listing: Any) -> dict[str, Any]:
    """The `ResolutionOut` fields of one listing (the caller checked it exists)."""
    from app.services.listings import remote_check

    jobs = await uow.resolution.read_jobs(listing.id)
    submissions, more = await uow.submissions.page(listing.id, after=None, limit=MAX_ATTEMPTS)
    total = await uow.submissions.count_for(listing.id) if more else None
    issues = await uow.resolution.issues_for(listing.id)
    repairs = await uow.resolution.repairs_for(listing.id)
    tasks = await uow.resolution.linked_tasks(listing.id)
    versions = await uow.resolution.versions_of(listing.id)
    cycles = await uow.resolution.cycles_since_human(listing.id)
    kinds = {t.task_id: t.kind for t in tasks}
    lanes = labels(
        issues,
        repaired={r.source_issue_id for r in repairs if r.source_issue_id is not None},
        task_kinds=kinds,
        critical_keys=await uow.product_facts.critical_keys(),
        fixing=fix_lanes(jobs),
        recategorizable=await recategorizable_listings(uow, issues),
    )
    live_tasks = {t.task_id: t.kind for t in tasks if t.status in LIVE_TASK}
    open_errors = [i for i in issues if i.status == "open" and i.severity == "error"]
    fix_job_ids = {j.id for j in jobs if _is_fix_job(j)}
    current = versions.get(listing.current_version_id) if listing.current_version_id else None
    latest = submissions[0] if submissions else None
    facts = ReadFacts(
        status=listing.status,
        status_reason=listing.status_reason,
        remote_check_status=getattr(listing, "remote_check_status", None),
        live_preview=any(
            j.status in LIVE_JOB
            and j.kind == "listing_status_sync"
            and (j.payload_json or {}).get("op") == "preview"
            for j in jobs
        ),
        live_status_sync=any(
            j.status in LIVE_JOB and j.kind == "listing_status_sync" for j in jobs
        ),
        live_fix_job=any(j.status in LIVE_JOB for j in jobs if j.id in fix_job_ids),
        latest_submission=None if latest is None else (latest.actor_type, latest.outcome),
        cycles=cycles,
        open_blocking=bool(open_errors),
        marketplace_tasks=tuple(
            sorted(t for t, k in live_tasks.items() if k == "permission_decision")
        ),
        seller_tasks=tuple(sorted(t for t, k in live_tasks.items() if k in SELLER_TASKS)),
        fix_ready=(
            current is not None
            and current.job_id in fix_job_ids
            and listing.approved_version_id != listing.current_version_id
        ),
        waiting_repairs=tuple(
            i.field_path for i in open_errors if lanes.get(i.id, ("",))[0] == "auto_repair"
        ),
        seller_action=seller_action(open_errors, lanes),
        answer_ready=(
            current is not None
            and current.origin == "edit"
            and current.job_id is None
            and listing.approved_version_id != listing.current_version_id
            and listing.status in ("validated", "needs_review")
            and latest is not None
            and latest.listing_version_id != listing.current_version_id
            and latest.outcome in ("rejected", "error")
        ),
    )
    state = state_of(facts)
    return {
        "state": state,
        "cycles_used": min(cycles, MAX_CYCLES),
        "remote_check": remote_check(listing),
        "attempts": attempts(
            jobs=jobs,
            submissions=submissions,
            issues=issues,
            repairs=repairs,
            tasks=tasks,
            versions=versions,
            lanes=lanes,
            total_submissions=total,
        ),
        "next_step": next_step(state, facts),
    }


async def resolution_for(principal: Principal, listing_id: uuid.UUID) -> dict[str, Any]:
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        listing = await uow.listings.get(listing_id)
        if listing is None:
            raise NotFoundError("listing_id")
        return await build(uow, listing)
