"""The Home summary (client feedback 11 Nov 2026, item 6): `GET /home/summary`.

Home used to add its numbers up in the browser from at most 1,000 listings, so a large brand
saw "+" counts and the tiles never matched the Products list. This read counts every row in the
database instead, with one definition per bucket.

Listing buckets (`listing_bucket`, checked in this order; every listing that is not archived
lands in exactly one, so the buckets sum to `total`):

- `live`: the marketplace confirmed it live (`marketplace_state` live).
- `partly_live`: the marketplace has some variants live (`partially_live`).
- `problem`: the marketplace rejected it, or a person acts next (`validated`, `needs_review`,
  `blocked`) while an open blocking issue sits on its current version (display state
  `needs_you`), or it is `blocked` or `failed`.
- `in_progress`: `generating` or `publishing`, or `published` and waiting on the marketplace
  (processing, in review, not yet confirmed). Nothing to do.
- `ready_to_approve`: `validated` or `needs_review` with no open blocking issue.
- `ready_to_publish`: `approved`.
- `inactive`: `published` and now inactive or removed at the marketplace.
- `draft`: `created`, not written yet.

Needs you (the caller's work; `total = blocked + questions + ready_to_approve +
ready_to_publish`):

- `questions`: open questions an open blocking issue waits on (`TaskOut.blocking`, so the Inbox
  shows the same number). Other open questions are `improve`: answering improves a listing.
- `blocked`: `problem` listings that wait on a fix. A listing whose every open blocking issue
  waits on an open question is the question's, and is not counted twice.
- `ready_to_approve`: only for a role that approves (`listing:approve`).
- `ready_to_publish`: only for a role that publishes (`listing:publish`), and only on a
  marketplace whose publishing is on (`app.core.flags.publish_enabled`). The approved listings
  of a paused marketplace are `publishing_paused`, for a role that approves or publishes: said,
  never counted, since nobody can send them.

`needs_you_items`: at most 5 things to do, in this order: blocked listings (with the newest open
blocking issue in the seller's words), blocking questions, listings to approve, listings to
publish; newest first within each. `activity`: at most 8 recent agent actions, newest first:
the jobs of `HomeJobReads.recent` (writing, sending, photo checks, imports, brand documents)
as completed, failed or pending, and the issues Kuinn fixed itself. `marketplaces`: each
marketplace's account and its one next step.

The read first re-checks the review queue (`recheck_queue`, as the Inbox reads do), so a stale
"ready to approve" opens its issues before it is counted; then every count runs in one READ ONLY
transaction under a 5 s budget.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Literal

from mkos_schemas.enums import Marketplace

from app.core.config import get_settings
from app.core.flags import marketplace_enabled, publish_enabled
from app.core.rbac import Action, Principal, allows
from app.repositories.home import (
    NEEDS_YOU_STATUSES,
    REVIEWABLE,
    BucketGroup,
    FixRow,
    IssueRow,
    JobRow,
    ListingRow,
    TaskRow,
    home_reads,
)
from app.repositories.transactions import unit_of_work

Bucket = Literal[
    "live",
    "partly_live",
    "problem",
    "in_progress",
    "ready_to_approve",
    "ready_to_publish",
    "inactive",
    "draft",
]
BUCKETS: Final[tuple[Bucket, ...]] = (
    "live",
    "partly_live",
    "in_progress",
    "problem",
    "ready_to_approve",
    "ready_to_publish",
    "draft",
    "inactive",
)
ITEMS_MAX: Final = 5
ACTIVITY_MAX: Final = 8
JOB_SCAN: Final = 30
ISSUE_TEXT_MAX: Final = 500
MARKETPLACE_NAME: Final = {"amazon": "Amazon", "flipkart": "Flipkart"}


def listing_bucket(status: str, marketplace_state: str, has_error: bool) -> Bucket:
    """The one bucket of a listing (module docstring)."""
    if marketplace_state == "live":
        return "live"
    if marketplace_state == "partially_live":
        return "partly_live"
    if marketplace_state == "rejected":
        return "problem"
    if status in NEEDS_YOU_STATUSES and has_error:
        return "problem"
    if status in ("generating", "publishing"):
        return "in_progress"
    if status in REVIEWABLE:
        return "ready_to_approve"
    if status == "approved":
        return "ready_to_publish"
    if status in ("blocked", "failed"):
        return "problem"
    if status == "published":
        return "inactive" if marketplace_state in ("inactive", "removed") else "in_progress"
    return "draft"


@dataclass(slots=True)
class Tally:
    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(BUCKETS, 0))
    blocked: int = 0
    """`problem` listings that wait on a fix (not only on an answer)."""

    def add(self, bucket: Bucket, n: int, *, question_only: bool) -> None:
        self.counts[bucket] += n
        if bucket == "problem" and not question_only:
            self.blocked += n

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def tally(groups: Iterable[BucketGroup]) -> tuple[Tally, dict[str, Tally]]:
    """The whole tenant's tally and one per marketplace."""
    whole = Tally()
    per: dict[str, Tally] = {m.value: Tally() for m in Marketplace}
    for g in groups:
        bucket = listing_bucket(g.status, g.marketplace_state, g.has_error)
        whole.add(bucket, g.n, question_only=g.question_only)
        per.setdefault(g.marketplace, Tally()).add(bucket, g.n, question_only=g.question_only)
    return whole, per


@dataclass(frozen=True, slots=True)
class Permissions:
    approve: bool
    publish: bool

    @classmethod
    def of(cls, role: str) -> Permissions:
        return cls(allows(role, Action.LISTING_APPROVE), allows(role, Action.LISTING_PUBLISH))


def needs_you_counts(
    per: Mapping[str, Tally],
    questions: int,
    improve: int,
    can: Permissions,
    publishing: Mapping[str, bool],
) -> dict[str, int]:
    blocked = sum(t.blocked for t in per.values())
    approve = sum(t.counts["ready_to_approve"] for t in per.values()) if can.approve else 0
    open_ = sum(t.counts["ready_to_publish"] for m, t in per.items() if publishing.get(m, True))
    paused = sum(
        t.counts["ready_to_publish"] for m, t in per.items() if not publishing.get(m, True)
    )
    publish = open_ if can.publish else 0
    return {
        "total": blocked + questions + approve + publish,
        "blocked": blocked,
        "questions": questions,
        "ready_to_approve": approve,
        "ready_to_publish": publish,
        "publishing_paused": paused if can.approve or can.publish else 0,
        "improve": improve,
    }


# ---- the words ----

REASON_TEXT: Final = {
    "human_task_open": "A question is waiting for your answer.",
    "validation_errors": "Checks found blocking issues.",
    "corrections_exhausted": "Kuinn could not fix every issue on its own.",
    "rejected": "A reviewer sent it back.",
    "generation_failed": "Writing the listing failed.",
    "publish_failed": "Sending it to the marketplace failed.",
    "publish_partial": "The marketplace did not accept some fields.",
    "marketplace_rejected": "The marketplace rejected it.",
    "marketplace_action_required": "The marketplace needs an action from you.",
    "publish_attempts_exhausted": "Every send attempt was used.",
}


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def blocked_issue_text(row: ListingRow, issue: IssueRow | None) -> str:
    """What is wrong with a blocked listing, in the seller's words: its newest open blocking
    issue (the seller-facing text first), else why its status says it stopped."""
    if issue is not None:
        return _clip(issue.plain_message or issue.message, ISSUE_TEXT_MAX)
    if row.marketplace_state == "rejected":
        return "The marketplace rejected this listing."
    if row.status_reason in REASON_TEXT:
        return REASON_TEXT[row.status_reason]
    if row.status == "failed":
        return "The last step failed."
    return "Checks stopped this listing."


def needs_you_items(
    blocked: Sequence[ListingRow],
    issues: Mapping[Any, IssueRow],
    questions: Sequence[TaskRow],
    approve: Sequence[ListingRow],
    publish: Sequence[ListingRow],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in blocked:
        issue = issues.get(row.listing_id)
        out.append(
            {
                **_listing_ref(row),
                "kind": "blocked",
                "action": "fix",
                "issue_id": issue.issue_id if issue else None,
                "issue": blocked_issue_text(row, issue),
                "issue_count": issue.open_count if issue else 0,
            }
        )
    for task in questions:
        out.append(
            {
                "kind": "question",
                "action": "answer",
                "product_id": task.product_id,
                "product_sku": task.sku,
                "product_title": task.title,
                "task_id": task.task_id,
                "issue": _clip(task.question, ISSUE_TEXT_MAX),
                "at": task.at,
            }
        )
    for row in approve:
        out.append(
            {
                **_listing_ref(row),
                "kind": "approve",
                "action": "approve",
                "issue": "Written and checked. Review it and approve.",
            }
        )
    for row in publish:
        name = MARKETPLACE_NAME.get(row.marketplace, row.marketplace)
        out.append(
            {
                **_listing_ref(row),
                "kind": "publish",
                "action": "publish",
                "issue": f"Approved. Ready to publish to {name}.",
            }
        )
    return out[:ITEMS_MAX]


def _listing_ref(row: ListingRow) -> dict[str, Any]:
    return {
        "product_id": row.product_id,
        "product_sku": row.sku,
        "product_title": row.title,
        "marketplace": row.marketplace,
        "listing_id": row.listing_id,
        "at": row.at,
    }


JobState = Literal["completed", "failed", "pending"]
_JOB_STATE: Final[dict[str, JobState]] = {
    "queued": "pending",
    "running": "pending",
    "succeeded": "completed",
    "partial": "completed",
    "failed": "failed",
    "dead_letter": "failed",
}
# (kind) -> feed kind and the words per state; `{p}` the product, `{m}` the marketplace
_JOB_TEXT: Final[dict[str, tuple[str, dict[JobState, str]]]] = {
    "listing_generate": (
        "write",
        {
            "pending": "Writing the {m} listing for {p}",
            "completed": "Wrote the {m} listing for {p}",
            "failed": "Could not write the {m} listing for {p}",
        },
    ),
    "listing_publish": (
        "send",
        {
            "pending": "Sending {p} to {m}",
            "completed": "Sent {p} to {m}",
            "failed": "Could not send {p} to {m}",
        },
    ),
    "image_compliance": (
        "photo_check",
        {
            "pending": "Checking a photo of {p}",
            "completed": "Checked a photo of {p}",
            "failed": "Could not check a photo of {p}",
        },
    ),
    "bulk_import": (
        "import",
        {
            "pending": "Importing your spreadsheet",
            "completed": "Imported your spreadsheet",
            "failed": "Could not import your spreadsheet",
        },
    ),
    "brand_embed": (
        "brand",
        {
            "pending": "Reading your brand material",
            "completed": "Read your brand material",
            "failed": "Could not read your brand material",
        },
    ),
}
_FIX_FIELD: Final = (
    ("title", "the title"),
    ("bullet", "a bullet point"),
    ("description", "the description"),
    ("keyword", "the search terms"),
    ("search_terms", "the search terms"),
    ("image", "a photo"),
    ("attributes.", "an attribute"),
)


def job_activity(job: JobRow) -> dict[str, Any] | None:
    state = _JOB_STATE.get(job.status)
    spec = _JOB_TEXT.get(job.kind)
    if state is None or spec is None:
        return None
    kind, words = spec
    at = job.finished_at or job.started_at or job.queued_at
    market = MARKETPLACE_NAME.get(job.marketplace or "", "the marketplace")
    text = words[state].format(p=job.title or "a product", m=market)
    return {
        "kind": kind,
        "state": state,
        "text": _clip(text.replace("the the marketplace", "the"), 300),
        "product_id": job.product_id,
        "product_title": job.title,
        "listing_id": job.listing_id,
        "marketplace": job.marketplace,
        "at": at,
    }


def fix_activity(fix: FixRow) -> dict[str, Any]:
    path = fix.field_path.lower()
    what = next((words for key, words in _FIX_FIELD if key in path), "a field")
    product = fix.title or "a product"
    market = MARKETPLACE_NAME.get(fix.marketplace, fix.marketplace)
    return {
        "kind": "fix",
        "state": "completed",
        "text": _clip(f"Fixed {what} of the {market} listing for {product}", 300),
        "product_id": fix.product_id,
        "product_title": fix.title,
        "listing_id": fix.listing_id,
        "marketplace": fix.marketplace,
        "at": fix.resolved_at,
    }


def activity(jobs: Iterable[JobRow], fixes: Iterable[FixRow]) -> list[dict[str, Any]]:
    """One feed, newest first, of at most `ACTIVITY_MAX`: pending, failed and completed alike."""
    items = [a for a in (job_activity(j) for j in jobs) if a is not None]
    items += [fix_activity(f) for f in fixes]
    items.sort(key=lambda a: a["at"], reverse=True)
    return items[:ACTIVITY_MAX]


NextStep = Literal["connect", "reconnect", "add_listing", "fix", "none"]


def next_step(account: str | None, t: Tally) -> NextStep:
    if account is None:
        return "connect"
    if account == "needs_reconnect":
        return "reconnect"
    if t.counts["problem"]:
        return "fix"
    if t.total == 0:
        return "add_listing"
    return "none"


def marketplace_rows(
    accounts: Mapping[str, str], per: Mapping[str, Tally], settings: Any
) -> list[dict[str, Any]]:
    out = []
    for m in Marketplace:
        status = accounts.get(m.value)
        out.append(
            {
                "marketplace": m.value,
                "enabled": marketplace_enabled(m, settings),
                "account": (
                    "not_connected"
                    if status is None
                    else ("needs_reconnect" if status == "needs_reconnect" else "connected")
                ),
                "publishing_enabled": publish_enabled(m, settings),
                "next_step": next_step(status, per.get(m.value, Tally())),
            }
        )
    return out


def _counts(t: Tally) -> dict[str, int]:
    return {**t.counts, "total": t.total}


async def get_summary(principal: Principal) -> dict[str, Any]:
    from app.repositories.uow import tenant_uow
    from app.services.listing_display import recheck_queue

    settings = get_settings()
    publishing = {m.value: publish_enabled(m, settings) for m in Marketplace}
    can = Permissions.of(principal.role)
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        await recheck_queue(uow, principal)  # a stale "passed" opens its issues first
    async with unit_of_work(principal.tenant_id, user_id=principal.user_id) as session:
        reads = home_reads(session, principal.tenant_id)
        await reads.listings.bounded()
        whole, per = tally(await reads.listings.bucket_groups())
        live_products = await reads.listings.live_products()
        questions, improve = await reads.tasks.open_counts()
        blocked = await reads.listings.blocked_rows(ITEMS_MAX)
        issues = await reads.issues.newest_open_errors(
            [(r.listing_id, r.current_version_id) for r in blocked]
        )
        tasks = await reads.tasks.blocking_rows(ITEMS_MAX) if len(blocked) < ITEMS_MAX else []
        room = ITEMS_MAX - len(blocked) - len(tasks)
        approve = (
            await reads.listings.ready_rows(approve=True, marketplaces=list(per), limit=room)
            if can.approve and room > 0
            else []
        )
        room -= len(approve)
        open_markets = [m for m, on in publishing.items() if on]
        publish = (
            await reads.listings.ready_rows(approve=False, marketplaces=open_markets, limit=room)
            if can.publish and room > 0
            else []
        )
        jobs = await reads.jobs.recent(JOB_SCAN)
        fixes = await reads.issues.recent_fixes(ACTIVITY_MAX)
        accounts = await reads.accounts.current()
        products = await reads.products.count()
    return {
        "as_of": datetime.now(UTC),
        "listings": {**_counts(whole), "live_products": live_products},
        "by_marketplace": [
            {"marketplace": m.value, "listings": per[m.value].total, **_market(per[m.value])}
            for m in Marketplace
        ],
        "needs_you": needs_you_counts(per, questions, improve, can, publishing),
        "needs_you_items": needs_you_items(blocked, issues, tasks, approve, publish),
        "activity": activity(jobs, fixes),
        "marketplaces": marketplace_rows(accounts, per, settings),
        "products": products,
    }


def _market(t: Tally) -> dict[str, int]:
    keys = ("live", "partly_live", "in_progress", "problem", "ready_to_approve", "ready_to_publish")
    return {k: t.counts[k] for k in keys}
