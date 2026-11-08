"""`ResolutionRepository` (SI11; ADR-0028 proposed): the resolution engine's reads across the
listing's jobs, tasks and issues. Every read joins tenant tables, so each carries this instance's
criterion for every table it names (`_criterion_for` + `_scoped_read`, as the analytics reads).

- `preview_breaker_open(account_id)` (CP-D24): a dry run on the account came back looking applied
  (Amazon may have stored it). Refused while a `marketplace_error_review` task of reason
  `preview_applied` is live on the account, or while a preview job of the account (its
  `result_json.account_id`) flagged
  `result_json.preview_applied` finished after the newest close of such a task (a crash between
  the job and its task).
- `open_severities(listing_id)`: the severities of the listing's open issue rows.
- `human_mark(listing_id)` (CP-D8): the newest human event on the listing: a review decision, an
  `edit` version, an answered or applied task linked through its issues, or a generate or publish
  job a member requested (`trigger` absent or `user`). `cycles_since_human` counts the listing's
  jobs marked `trigger = "resolution"` queued after it, any status (invariant 9).
- `recurrence_resolutions(ids)`: the resolution of each closed row a recurrence points at.
- The resolution read (T20): `read_jobs`, `issues_for`, `repairs_for`, `linked_tasks`,
  `versions_of`, `repaired_issue_ids`, `task_kinds`, `fix_jobs` (newest first, bounded).
"""

from __future__ import annotations

import builtins
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import func, or_, select

from app.db.models.issues import HumanTask, ListingIssue
from app.db.models.listings import ListingVersion, ReviewDecision
from app.db.models.ops import JobRun
from app.db.models.resolution import ListingRepair
from app.repositories.base import TenantScopedRepository

BREAKER_KIND: Final = "marketplace_error_review"
READ_JOB_KINDS: Final = ("listing_status_sync", "listing_generate", "listing_publish")
BREAKER_TARGET: Final = "account.preview_applied"
FIX_JOB_STATUSES: Final = ("queued", "running", "succeeded")


@dataclass(frozen=True, slots=True)
class LinkedTask:
    issue_id: uuid.UUID
    task_id: uuid.UUID
    kind: str
    status: str
    target_path: str | None
    answered_at: datetime | None


@dataclass(frozen=True, slots=True)
class VersionRow:
    id: uuid.UUID
    version: int
    origin: str
    job_id: uuid.UUID | None
    approved_at: datetime | None


class ResolutionRepository(TenantScopedRepository[ListingRepair]):
    model = ListingRepair

    async def preview_breaker_open(self, account_id: uuid.UUID) -> bool:
        t = HumanTask
        live = select(t.id).where(
            self._criterion_for(HumanTask),
            t.marketplace_account_id == account_id,
            t.kind == BREAKER_KIND,
            t.target_path == BREAKER_TARGET,
            t.status.in_(("open", "answered")),
        )
        if (await self.db.execute(self._scoped_read(live.limit(1)))).first() is not None:
            return True
        closed = select(func.max(t.applied_at)).where(
            self._criterion_for(HumanTask),
            t.marketplace_account_id == account_id,
            t.kind == BREAKER_KIND,
            t.target_path == BREAKER_TARGET,
        )
        last_close = (await self.db.execute(self._scoped_read(closed))).scalar_one_or_none()
        j = JobRun
        flagged = select(j.id).where(
            self._criterion_for(JobRun),
            j.kind == "listing_status_sync",
            j.result_json["account_id"].as_string() == str(account_id),
            j.result_json["preview_applied"].as_boolean().is_(True),
        )
        if last_close is not None:
            flagged = flagged.where(j.finished_at > last_close)
        return (await self.db.execute(self._scoped_read(flagged.limit(1)))).first() is not None

    async def open_severities(self, listing_id: uuid.UUID) -> set[str]:
        i = ListingIssue
        stmt = (
            select(i.severity)
            .where(
                self._criterion_for(ListingIssue), i.listing_id == listing_id, i.status == "open"
            )
            .distinct()
        )
        return {str(s) for s in (await self.db.execute(self._scoped_read(stmt))).scalars()}

    async def has_open_preview_error(self, listing_id: uuid.UUID, version_id: uuid.UUID) -> bool:
        """An open `amazon_adapter` ERROR of the dry run on `version_id` (the approve gate, T8)."""
        i = ListingIssue
        stmt = select(i.id).where(
            self._criterion_for(ListingIssue),
            i.listing_id == listing_id,
            i.listing_version_id == version_id,
            i.status == "open",
            i.severity == "error",
            i.source == "amazon_adapter",
            i.dedupe_key.like("%|preview"),
        )
        return (await self.db.execute(self._scoped_read(stmt.limit(1)))).first() is not None

    async def human_mark(self, listing_id: uuid.UUID) -> datetime | None:
        d, v, t, i, j = ReviewDecision, ListingVersion, HumanTask, ListingIssue, JobRun
        decided = select(func.max(d.decided_at)).where(
            self._criterion_for(ReviewDecision), d.listing_id == listing_id
        )
        edited = select(func.max(v.created_at)).where(
            self._criterion_for(ListingVersion), v.listing_id == listing_id, v.origin == "edit"
        )
        answered = (
            select(func.max(func.greatest(t.answered_at, t.applied_at)))
            .join(i, (i.human_task_id == t.id) & (i.tenant_id == t.tenant_id))
            .where(
                self._criterion_for(HumanTask),
                self._criterion_for(ListingIssue),
                i.listing_id == listing_id,
                t.status.in_(("answered", "applied")),
            )
        )
        requested = select(func.max(j.queued_at)).where(
            self._criterion_for(JobRun),
            j.listing_id == listing_id,
            j.kind.in_(("listing_generate", "listing_publish")),
            j.created_by.is_not(None),
            or_(
                j.payload_json["trigger"].as_string().is_(None),
                j.payload_json["trigger"].as_string() == "user",
            ),
        )
        marks = []
        for stmt in (decided, edited, answered, requested):
            marks.append((await self.db.execute(self._scoped_read(stmt))).scalar_one_or_none())
        found = [m for m in marks if m is not None]
        return max(found) if found else None

    async def cycles_since_human(self, listing_id: uuid.UUID) -> int:
        j = JobRun
        mark = await self.human_mark(listing_id)
        stmt = select(func.count()).where(
            self._criterion_for(JobRun),
            j.listing_id == listing_id,
            j.payload_json["trigger"].as_string() == "resolution",
        )
        if mark is not None:
            stmt = stmt.where(j.queued_at > mark)
        return int((await self.db.execute(self._scoped_read(stmt))).scalar_one())

    async def recurrence_resolutions(self, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, str]:
        if not ids:
            return {}
        i = ListingIssue
        stmt = select(i.id, i.resolution).where(
            self._criterion_for(ListingIssue), i.id.in_(builtins.list(ids))
        )
        rows = (await self.db.execute(self._scoped_read(stmt))).all()
        return {row.id: row.resolution for row in rows if row.resolution is not None}

    # ---- the resolution read (T20) ----

    async def read_jobs(self, listing_id: uuid.UUID, limit: int = 100) -> builtins.list[JobRun]:
        """The listing's dry-run, generate and publish jobs, newest first."""
        j = JobRun
        stmt = (
            select(j)
            .where(
                self._criterion_for(JobRun), j.listing_id == listing_id, j.kind.in_(READ_JOB_KINDS)
            )
            .order_by(j.queued_at.desc(), j.id.desc())
            .limit(limit)
        )
        return builtins.list((await self.db.execute(self._scoped_read(stmt))).scalars())

    async def fix_jobs(
        self, listing_ids: Sequence[uuid.UUID], limit: int = 500
    ) -> builtins.list[JobRun]:
        """The listings' resolution fix jobs (`listing_generate`, `trigger = "resolution"`)
        that are live or succeeded, newest first: the issues they cover read `auto_content` or
        `ai_field` (fix round 1)."""
        if not listing_ids:
            return []
        j = JobRun
        stmt = (
            select(j)
            .where(
                self._criterion_for(JobRun),
                j.listing_id.in_(builtins.list(listing_ids)),
                j.kind == "listing_generate",
                j.status.in_(FIX_JOB_STATUSES),
                j.payload_json["trigger"].astext == "resolution",
            )
            .order_by(j.queued_at.desc(), j.id.desc())
            .limit(limit)
        )
        return builtins.list((await self.db.execute(self._scoped_read(stmt))).scalars())

    async def issues_for(
        self, listing_id: uuid.UUID, limit: int = 500
    ) -> builtins.list[ListingIssue]:
        i = ListingIssue
        stmt = (
            select(i)
            .where(self._criterion_for(ListingIssue), i.listing_id == listing_id)
            .order_by(i.first_seen_at.desc(), i.id.desc())
            .limit(limit)
        )
        return builtins.list((await self.db.execute(self._scoped_read(stmt))).scalars())

    async def repairs_for(
        self, listing_id: uuid.UUID, limit: int = 200
    ) -> builtins.list[ListingRepair]:
        r = ListingRepair
        stmt = (
            self._select()
            .where(r.listing_id == listing_id)
            .order_by(r.created_at.desc(), r.id.desc())
            .limit(limit)
        )
        return builtins.list((await self.db.execute(stmt)).scalars())

    async def linked_tasks(self, listing_id: uuid.UUID) -> builtins.list[LinkedTask]:
        """Each task linked to one of the listing's issue rows, with the row it is linked to."""
        t, i = HumanTask, ListingIssue
        stmt = (
            select(i.id, t.id, t.kind, t.status, t.target_path, t.answered_at)
            .join(t, (t.id == i.human_task_id) & (t.tenant_id == i.tenant_id))
            .where(
                self._criterion_for(ListingIssue),
                self._criterion_for(HumanTask),
                i.listing_id == listing_id,
            )
            .order_by(t.created_at.desc(), t.id.desc())
            .limit(500)
        )
        rows = (await self.db.execute(self._scoped_read(stmt))).all()
        return [LinkedTask(*row) for row in rows]

    async def versions_of(self, listing_id: uuid.UUID) -> dict[uuid.UUID, VersionRow]:
        v = ListingVersion
        stmt = select(v.id, v.version, v.origin, v.job_id, v.approved_at).where(
            self._criterion_for(ListingVersion), v.listing_id == listing_id
        )
        rows = (await self.db.execute(self._scoped_read(stmt))).all()
        return {row[0]: VersionRow(*row) for row in rows}

    async def repaired_issue_ids(self, issue_ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
        """The issue rows a repair (live or retired) was written for."""
        if not issue_ids:
            return set()
        r = ListingRepair
        stmt = (
            self._select()
            .where(r.source_issue_id.in_(builtins.list(issue_ids)))
            .with_only_columns(r.source_issue_id)
            .distinct()
        )
        return {row for row in (await self.db.execute(stmt)).scalars() if row is not None}

    async def task_kinds(self, task_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, tuple[str, str]]:
        """task id -> (kind, status)."""
        if not task_ids:
            return {}
        t = HumanTask
        stmt = select(t.id, t.kind, t.status).where(
            self._criterion_for(HumanTask), t.id.in_(builtins.list(task_ids))
        )
        rows: Any = (await self.db.execute(self._scoped_read(stmt))).all()
        return {row[0]: (row[1], row[2]) for row in rows}
