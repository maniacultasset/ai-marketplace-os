"""The resolution read (SI11 T20; ADR-0028 proposed; P:B8; CP-D18): `GET /api/v1/listings/{id}/
resolution`, what Kuinn checked, changed and still needs for one listing, and the lane and cause
of each issue row the inbox shows.

- `labels(rows, ...)` (pure): an issue's lane and cause. A row a repair was written for, or one
  closed by a repair, is `auto_repair`; a row linked to a task takes the task's lane (a
  `permission_decision` is the marketplace's, a `marketplace_error_review` staff's, a fact or
  image question the seller's); any other row the planner's own rules (`plan.label`). WARNINGs
  carry no lane.
- `state_of(facts)` (pure): the precedence `live` > `fixing` > `checking` > `resubmitted` >
  `stopped` > `needs_marketplace` > `needs_you` > `fix_ready` > `idle`.
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

from app.core.errors import NotFoundError
from app.core.rbac import Principal
from app.repositories.resolution import LinkedTask, VersionRow
from app.repositories.uow import TenantUnitOfWork, tenant_uow
from app.services.resolution.plan import (
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
TASK_LANES: Final[Mapping[str, Lane]] = {
    "permission_decision": "marketplace",
    "marketplace_error_review": "staff",
    **dict.fromkeys(SELLER_TASKS, "seller"),
}
PRECHECK_VERDICTS: Final = frozenset({"clean", "issues", "failed"})
OPEN_SUBMISSION: Final = frozenset({"pending", "accepted", "partially_accepted"})
PREVIEW_SUFFIX: Final = "|preview"


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


def labels(
    rows: Iterable[Any],
    *,
    repaired: Iterable[uuid.UUID] = (),
    task_kinds: Mapping[uuid.UUID, str] | None = None,
    critical_keys: frozenset[str] = frozenset(),
) -> dict[uuid.UUID, tuple[Lane, Cause]]:
    """issue id -> (lane, cause); WARNING rows are left out."""
    repaired_ids = set(repaired)
    kinds = task_kinds or {}
    ctx = PlanContext(critical_keys=critical_keys)
    out: dict[uuid.UUID, tuple[Lane, Cause]] = {}
    for row in rows:
        if row.severity != "error":
            continue
        view = _view(row)
        lane: Lane | None = None
        if row.id in repaired_ids or (row.action_taken or "").startswith("repair:"):
            lane = "auto_repair"
        elif row.human_task_id is not None and kinds.get(row.human_task_id) in TASK_LANES:
            lane = TASK_LANES[kinds[row.human_task_id]]
        if lane is not None:
            out[row.id] = (lane, cause_of(view, lane, {}))
            continue
        found = label(view, ctx)
        if found is not None:
            out[row.id] = found
    return out


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
    return labels(
        errors,
        repaired=repaired,
        task_kinds={k: v[0] for k, v in tasks.items()},
        critical_keys=await uow.product_facts.critical_keys(),
    )


# ---- state ----


@dataclass(frozen=True, slots=True)
class ReadFacts:
    status: str
    status_reason: str | None = None
    remote_check_status: str | None = None
    live_preview: bool = False
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


def state_of(f: ReadFacts) -> str:
    if f.status == "published":
        return "live"
    if f.live_fix_job:
        return "fixing"
    if f.remote_check_status == "pending" or f.live_preview:
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
    if f.seller_tasks:
        return "needs_you"
    if f.fix_ready:
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
    if state == "needs_you":
        return {
            "kind": "answer",
            "task_ids": list(f.seller_tasks)[:20],
            "text": "Answer the open question so Kuinn can fix the listing.",
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


def _submission(row: Any) -> _Attempt:
    return _Attempt(
        "submission",
        start=row.created_at,
        at=row.created_at,
        number=int(row.attempt),
        version_id=row.listing_version_id,
        actor="system" if row.actor_type == "system" else "user",
        outcome=row.outcome,
    )


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
) -> list[dict[str, Any]]:
    found = [a for a in (_precheck(j) for j in jobs) if a is not None]
    found += [_submission(s) for s in submissions]
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
    submissions, _more = await uow.submissions.page(listing.id, after=None, limit=MAX_ATTEMPTS)
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
        ),
        "next_step": next_step(state, facts),
    }


async def resolution_for(principal: Principal, listing_id: uuid.UUID) -> dict[str, Any]:
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        listing = await uow.listings.get(listing_id)
        if listing is None:
            raise NotFoundError("listing_id")
        return await build(uow, listing)
