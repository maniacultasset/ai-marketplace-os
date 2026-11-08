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
"""

from __future__ import annotations

import builtins
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Final

from sqlalchemy import exists, func, or_, select

from app.db.models.issues import HumanTask, ListingIssue
from app.db.models.listings import ListingVersion, ReviewDecision
from app.db.models.ops import JobRun
from app.db.models.resolution import ListingRepair
from app.repositories.base import TenantScopedRepository

BREAKER_KIND: Final = "marketplace_error_review"
BREAKER_TARGET: Final = "account.preview_applied"


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
        stmt = select(
            exists().where(
                self._criterion_for(ListingIssue),
                i.listing_id == listing_id,
                i.listing_version_id == version_id,
                i.status == "open",
                i.severity == "error",
                i.source == "amazon_adapter",
                i.dedupe_key.like("%|preview"),
            )
        )
        return bool((await self.db.execute(self._scoped_read(stmt))).scalar())

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
