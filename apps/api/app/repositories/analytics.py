"""`AnalyticsRepository`: the four operational metrics for one tenant and a UTC range (S23;
spec L4773, L5370; PLAN D1 formulas; CHANGELOG 1.1.0 "Analytics").

Every read names `tenant_id = <this tenant>` for each tenant table it touches (`_scoped_read`
refuses one that does not; RLS is the backstop) and groups by marketplace:

- `volume`: `listing_versions` of origin `generation` or `regeneration` by `created_at`, joined to
  `listings` for the marketplace (`edit` and `import` versions are not generations).
- `decisions`: `review_decisions` `approve` / `reject` by `decided_at`, joined to `listings`
  (`regenerate_requested` is neither).
- `validation`: first-attempt Rules Engine runs (`source = 'rules_engine' AND attempt = 1`) by
  `created_at`, passed of total, on the run's own marketplace.
- `publish`: `listing_submissions` answered in range (`responded_at`): succeeded = `accepted`,
  `live`, `superseded`; failed = `partially_accepted`, `rejected`, and an `error` whose
  ErrorReport is not `retryable` (a retryable error is the publish infrastructure retrying, not a
  publish outcome); `pending` has no `responded_at`.

`bounded()` makes the request's transaction READ ONLY with one 5 s `transaction_timeout` and a 5 s
`statement_timeout`, before the metric reads. The partial-index constants are rendered as SQL
literals (never bound parameters), so a generic plan still matches the 0009 index predicates.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Final

from sqlalchemy import Select, and_, false, func, literal_column, not_, select, text

from app.db.models.listings import Listing, ListingVersion, ReviewDecision
from app.db.models.ops import ListingSubmission
from app.db.models.runs import ValidationRun
from app.repositories.base import TenantScopedRepository

GENERATED_ORIGINS: Final = ("generation", "regeneration")
SUCCEEDED: Final = frozenset({"accepted", "live", "superseded"})
FAILED: Final = frozenset({"partially_accepted", "rejected", "error"})
REQUEST_BUDGET: Final = "5s"


def _lit(value: str | int) -> Any:
    """A SQL literal (not a bind): partial-index predicates must match under generic plans."""
    if isinstance(value, int):
        return literal_column(str(int(value)))
    if "'" in value:
        raise ValueError("literal values are fixed identifiers")
    return literal_column(f"'{value}'")


class AnalyticsRepository(TenantScopedRepository[ListingVersion]):
    model = ListingVersion

    async def bounded(self) -> None:
        """One budget for the whole request: READ ONLY, a 5 s transaction and statement cap."""
        await self.db.execute(text("SET TRANSACTION READ ONLY"))
        await self.db.execute(text(f"SET LOCAL transaction_timeout = '{REQUEST_BUDGET}'"))
        await self.db.execute(text(f"SET LOCAL statement_timeout = '{REQUEST_BUDGET}'"))

    def _listing_join(self, child: Any) -> Any:
        return and_(Listing.tenant_id == child.tenant_id, Listing.id == child.listing_id)

    def volume_stmt(self, lo: datetime, hi: datetime) -> Select[Any]:
        stmt = (
            select(Listing.marketplace, func.count())
            .select_from(ListingVersion)
            .join(Listing, self._listing_join(ListingVersion))
            .where(
                self._criterion_for(ListingVersion),
                self._criterion_for(Listing),
                ListingVersion.origin.in_([_lit(o) for o in GENERATED_ORIGINS]),
                ListingVersion.created_at >= lo,
                ListingVersion.created_at < hi,
            )
            .group_by(Listing.marketplace)
        )
        return self._scoped_read(stmt)

    def decisions_stmt(self, lo: datetime, hi: datetime) -> Select[Any]:
        stmt = (
            select(Listing.marketplace, ReviewDecision.decision, func.count())
            .select_from(ReviewDecision)
            .join(Listing, self._listing_join(ReviewDecision))
            .where(
                self._criterion_for(ReviewDecision),
                self._criterion_for(Listing),
                ReviewDecision.decision.in_([_lit("approve"), _lit("reject")]),
                ReviewDecision.decided_at >= lo,
                ReviewDecision.decided_at < hi,
            )
            .group_by(Listing.marketplace, ReviewDecision.decision)
        )
        return self._scoped_read(stmt)

    def validation_stmt(self, lo: datetime, hi: datetime) -> Select[Any]:
        stmt = (
            select(ValidationRun.marketplace, ValidationRun.passed, func.count())
            .where(
                self._criterion_for(ValidationRun),
                ValidationRun.source == _lit("rules_engine"),
                ValidationRun.attempt == _lit(1),
                ValidationRun.created_at >= lo,
                ValidationRun.created_at < hi,
            )
            .group_by(ValidationRun.marketplace, ValidationRun.passed)
        )
        return self._scoped_read(stmt)

    def publish_stmt(self, lo: datetime, hi: datetime) -> Select[Any]:
        retryable = func.coalesce(
            ListingSubmission.error_report_json["retryable"].as_boolean(), false()
        )
        stmt = (
            select(ListingSubmission.marketplace, ListingSubmission.outcome, func.count())
            .where(
                self._criterion_for(ListingSubmission),
                ListingSubmission.responded_at.is_not(None),
                ListingSubmission.responded_at >= lo,
                ListingSubmission.responded_at < hi,
                ListingSubmission.outcome.in_([_lit(o) for o in sorted(SUCCEEDED | FAILED)]),
                not_(and_(ListingSubmission.outcome == _lit("error"), retryable)),
            )
            .group_by(ListingSubmission.marketplace, ListingSubmission.outcome)
        )
        return self._scoped_read(stmt)

    async def volume(self, lo: datetime, hi: datetime) -> dict[str, int]:
        rows = await self.db.execute(self.volume_stmt(lo, hi))
        return {str(m): int(n) for m, n in rows}

    async def decisions(self, lo: datetime, hi: datetime) -> dict[str, tuple[int, int]]:
        """marketplace → (approvals, rejections)."""
        out: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for marketplace, decision, n in await self.db.execute(self.decisions_stmt(lo, hi)):
            out[str(marketplace)][0 if decision == "approve" else 1] += int(n)
        return {m: (a, r) for m, (a, r) in out.items()}

    async def validation(self, lo: datetime, hi: datetime) -> dict[str, tuple[int, int]]:
        """marketplace → (passed, total)."""
        out: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for marketplace, passed, n in await self.db.execute(self.validation_stmt(lo, hi)):
            out[str(marketplace)][1] += int(n)
            if passed:
                out[str(marketplace)][0] += int(n)
        return {m: (p, t) for m, (p, t) in out.items()}

    async def publish(self, lo: datetime, hi: datetime) -> dict[str, tuple[int, int]]:
        """marketplace → (succeeded, failed)."""
        out: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for marketplace, outcome, n in await self.db.execute(self.publish_stmt(lo, hi)):
            out[str(marketplace)][0 if outcome in SUCCEEDED else 1] += int(n)
        return {m: (s, f) for m, (s, f) in out.items()}
