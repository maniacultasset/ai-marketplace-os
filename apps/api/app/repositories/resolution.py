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
"""

from __future__ import annotations

import uuid
from typing import Final

from sqlalchemy import exists, func, select

from app.db.models.issues import HumanTask, ListingIssue
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
