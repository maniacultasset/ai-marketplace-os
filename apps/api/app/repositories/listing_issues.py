"""`listing_issues` (DATABASE.md §5.7 :1304-1336; S19 T17): the seller's issue rows.

- `upsert_open(new)`: `INSERT … ON CONFLICT (listing_id, dedupe_key) WHERE status='open' DO UPDATE
  SET occurrences + 1, last_seen_at, last_validation_run_id, message, value_submitted` (the
  `li_one_open` index); a fresh row links `recurrence_of_id` to the latest closed row with the
  same key. Returns the row and whether it was new.
- `close_missing(listing_id, source, keep_keys, resolution, ...)`: closes this listing's open rows
  of one source whose key is not in `keep_keys`. `field_paths=None` is full scope (rules,
  schema and vision runs); a list closes only rows on those paths (an adapter result covers the
  fields it sent); an empty list closes nothing.
- `page(filters, after=, limit=)`: the inbox, newest first by `(created_at, id)` (the open-inbox
  and all-status indexes).
- `open_row(listing_id, key)` and `close_open(issue_id, resolution)` (SI4): the open row of a key,
  and a system compare-and-set close in the status `closed_status(resolution)` names.
- `lock(issue_id)` and `dismiss(issue_id, user_id)`: the dismissal is a compare-and-set that only
  matches an open warning; `listing_issues_guard` (GRD01) is the backstop.

Every write is a Core statement scoped to the repository's tenant (no raw SQL INSERT).
"""

from __future__ import annotations

import builtins
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal

from sqlalchemy import exists, func, literal, literal_column, not_, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db.guard import TENANT_INFO_KEY, CrossTenantQueryError
from app.db.integrity import map_db_error, to_conflict
from app.db.models.issues import HumanTask, ListingIssue
from app.db.models.listings import Listing
from app.repositories.base import TenantScopedRepository

PAGE_LIMIT_MAX: Final = 200
OPEN: Final = "open"
# SI11 (CP-D4): the dry run's rows (`services.issues.PREVIEW_SUFFIX`); `|` is not a LIKE wildcard
PREVIEW_KEY_LIKE: Final = "%|preview"


@dataclass(frozen=True, slots=True)
class NewIssue:
    listing_id: uuid.UUID
    marketplace: str
    source: str
    field_path: str
    error_code: str
    error_class: str
    retry_class: str
    severity: str
    message: str
    dedupe_key: str
    listing_version_id: uuid.UUID | None = None
    validation_run_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    listing_item_id: uuid.UUID | None = None
    auto_fixable: bool = False
    fix_action: str | None = None
    rule_id: uuid.UUID | None = None
    marketplace_code: str | None = None
    suggestion: str | None = None
    value_submitted: str | None = None


@dataclass(frozen=True, slots=True)
class IssueFilter:
    status: str = OPEN
    listing_id: uuid.UUID | None = None
    source: str | None = None
    error_code: str | None = None
    marketplace: str | None = None


def closed_status(resolution: str) -> str:
    """The closed status a resolution takes: `auto_fixed` only for `deterministic_fix`
    (ck_listing_issues_auto_fixed); every other one, `ai_correction` included, is `resolved`."""
    return "auto_fixed" if resolution == "deterministic_fix" else "resolved"


class ListingIssueRepository(TenantScopedRepository[ListingIssue]):
    model = ListingIssue

    def _own_session(self) -> None:
        if self.db.info.get(TENANT_INFO_KEY) != self.tenant_id:
            raise CrossTenantQueryError("listing_issues write for another tenant's unit of work")

    async def _run(self, stmt: Any) -> Any:
        try:
            return await self.db.execute(stmt, execution_options={"populate_existing": True})
        except IntegrityError as exc:
            raise to_conflict(exc) from exc
        except DBAPIError as exc:
            mapped = map_db_error(exc)
            if mapped is not None:
                raise mapped from exc
            raise

    async def latest_closed(self, listing_id: uuid.UUID, key: str) -> ListingIssue | None:
        stmt = (
            self._select()
            .where(
                ListingIssue.listing_id == listing_id,
                ListingIssue.dedupe_key == key,
                ListingIssue.status != OPEN,
            )
            .order_by(ListingIssue.resolved_at.desc(), ListingIssue.id.desc())
            .limit(1)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def task_ids_for_job(self, job_id: uuid.UUID) -> builtins.list[uuid.UUID]:
        """The human tasks linked to the issues a job raised (S21: `human_resolution`)."""
        stmt = (
            self._select()
            .where(ListingIssue.job_id == job_id, ListingIssue.human_task_id.is_not(None))
            .with_only_columns(ListingIssue.human_task_id)
        )
        return [row for row in (await self.db.execute(stmt)).scalars() if row is not None]

    async def link_task(self, listing_id: uuid.UUID, job_id: uuid.UUID, task_id: uuid.UUID) -> int:
        """Link the open issues a job raised on a listing to a human task (S21 CP8)."""
        self._own_session()
        criterion = self._tenant_criterion(include_platform=False)
        stmt = self._scoped_write(
            update(ListingIssue)
            .where(
                criterion,
                ListingIssue.listing_id == listing_id,
                ListingIssue.job_id == job_id,
                ListingIssue.status == OPEN,
            )
            .values(human_task_id=task_id)
        ).returning(ListingIssue.id)
        return len((await self._run(stmt)).all())

    async def unresolved_auth(self, listing_ids: Sequence[uuid.UUID]) -> builtins.list[uuid.UUID]:
        """The open `AUTH_EXPIRED` issues of `listing_ids` that no `answered`/`applied` task
        covers (the 0004 publish gate's anti-loop rule would refuse their listing's publish)."""
        if not listing_ids:
            return []
        covered = exists(
            select(HumanTask.id).where(
                HumanTask.tenant_id == ListingIssue.tenant_id,
                HumanTask.id == ListingIssue.human_task_id,
                HumanTask.status.in_(("answered", "applied")),
            )
        )
        stmt = (
            self._select()
            .where(
                ListingIssue.listing_id.in_(tuple(listing_ids)),
                ListingIssue.status == OPEN,
                ListingIssue.error_code == "AUTH_EXPIRED",
                ~covered,
            )
            .with_only_columns(ListingIssue.id)
            .order_by(ListingIssue.id)
        )
        return builtins.list((await self.db.execute(stmt)).scalars())

    async def open_errors(
        self,
        listing_id: uuid.UUID,
        sources: Sequence[str],
        *,
        channel: Literal["live", "preview"] | None = None,
    ) -> builtins.list[ListingIssue]:
        """SI4 A13: this listing's open blocking (`error`) rows of the given sources, oldest
        first: the rows a question may stand behind (a warning never asks, Approval record).
        SI11: `channel` keeps only the dry run's rows (`preview`) or only the others (`live`)."""
        stmt = (
            self._select()
            .where(
                ListingIssue.listing_id == listing_id,
                ListingIssue.status == OPEN,
                ListingIssue.severity == "error",
                ListingIssue.source.in_(tuple(sources)),
            )
            .order_by(ListingIssue.created_at, ListingIssue.id)
        )
        if channel is not None:
            is_preview = ListingIssue.dedupe_key.like(PREVIEW_KEY_LIKE)
            stmt = stmt.where(is_preview if channel == "preview" else not_(is_preview))
        return builtins.list((await self.db.execute(stmt)).scalars())

    async def open_error_counts(
        self, versions: Sequence[tuple[uuid.UUID, uuid.UUID | None]]
    ) -> dict[uuid.UUID, int]:
        """The open blocking (`error`) rows per listing, of the given `(listing_id,
        current_version_id)` pairs: one grouped query for a whole page (the display state). A
        row of an older version is history and is not counted; a row with no version (a
        listing-level answer) is."""
        if not versions:
            return {}
        current = dict(versions)
        stmt = (
            self._select()
            .where(
                ListingIssue.listing_id.in_(tuple(current)),
                ListingIssue.status == OPEN,
                ListingIssue.severity == "error",
            )
            .with_only_columns(
                ListingIssue.listing_id, ListingIssue.listing_version_id, func.count()
            )
            .group_by(ListingIssue.listing_id, ListingIssue.listing_version_id)
        )
        counts: dict[uuid.UUID, int] = {}
        for listing_id, version_id, n in (await self.db.execute(stmt)).all():
            if version_id is None or version_id == current.get(listing_id):
                counts[listing_id] = counts.get(listing_id, 0) + int(n)
        return counts

    async def open_adapter_category_issue(
        self, listing_id: uuid.UUID, version_id: uuid.UUID | None
    ) -> uuid.UUID | None:
        """SI13 T8: the open marketplace `CATEGORY_MISMATCH` row of the listing's current version
        (the reason a rejected listing may still be recategorised), or None."""
        if version_id is None:
            return None
        stmt = (
            self._select()
            .where(
                ListingIssue.listing_id == listing_id,
                ListingIssue.listing_version_id == version_id,
                ListingIssue.status == OPEN,
                ListingIssue.severity == "error",
                ListingIssue.error_code == "CATEGORY_MISMATCH",
                ListingIssue.source.in_(("amazon_adapter", "flipkart_adapter")),
            )
            .with_only_columns(ListingIssue.id)
            .order_by(ListingIssue.created_at, ListingIssue.id)
            .limit(1)
        )
        found: uuid.UUID | None = (await self.db.execute(stmt)).scalar_one_or_none()
        return found

    async def open_by_ids(
        self, listing_id: uuid.UUID, issue_ids: Sequence[uuid.UUID]
    ) -> builtins.list[ListingIssue]:
        """SI11 T15: the listing's still open rows among `issue_ids`, oldest first."""
        if not issue_ids:
            return []
        stmt = (
            self._select()
            .where(
                ListingIssue.listing_id == listing_id,
                ListingIssue.status == OPEN,
                ListingIssue.id.in_(builtins.list(issue_ids)),
            )
            .order_by(ListingIssue.created_at, ListingIssue.id)
        )
        return builtins.list((await self.db.execute(stmt)).scalars())

    async def listings_for_task(
        self, task_id: uuid.UUID, sources: Sequence[str]
    ) -> builtins.list[uuid.UUID]:
        """SI11 T17: the listings with a row of `sources` linked to the task, in id order."""
        stmt = (
            self._select()
            .where(ListingIssue.human_task_id == task_id, ListingIssue.source.in_(tuple(sources)))
            .with_only_columns(ListingIssue.listing_id)
            .distinct()
            .order_by(ListingIssue.listing_id)
        )
        return builtins.list((await self.db.execute(stmt)).scalars())

    async def answered_paths(
        self, listing_id: uuid.UUID, sources: Sequence[str], kinds: Sequence[str]
    ) -> builtins.list[str]:
        """SI11 T17: the target paths of the answered questions behind this listing's open rows of
        `sources` (the facts one fact-only version records), sorted."""
        answered = (
            select(HumanTask.target_path)
            .where(
                HumanTask.tenant_id == ListingIssue.tenant_id,
                HumanTask.id == ListingIssue.human_task_id,
                HumanTask.status.in_(("answered", "applied")),
                HumanTask.kind.in_(tuple(kinds)),
                HumanTask.target_path.is_not(None),
            )
            .scalar_subquery()
        )
        stmt = (
            self._select()
            .where(
                ListingIssue.listing_id == listing_id,
                ListingIssue.status == OPEN,
                ListingIssue.source.in_(tuple(sources)),
                ListingIssue.human_task_id.is_not(None),
            )
            .with_only_columns(answered)
            .distinct()
        )
        return sorted(p for p in (await self.db.execute(stmt)).scalars() if p is not None)

    async def blocking_task_ids(self, task_ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
        """QA 2026-11-09: the tasks of `task_ids` an open ERROR row is linked to (the question
        blocks a listing: `issue_questions` opens it for that row, or reuses it)."""
        if not task_ids:
            return set()
        stmt = (
            self._select()
            .where(
                ListingIssue.human_task_id.in_(list(task_ids)),
                ListingIssue.status == "open",
                ListingIssue.severity == "error",
            )
            .with_only_columns(ListingIssue.human_task_id)
            .distinct()
        )
        return {row for row in (await self.db.execute(stmt)).scalars() if row is not None}

    async def live_task_ids(self, listing_id: uuid.UUID) -> builtins.list[uuid.UUID]:
        """SI4 A12: the distinct human tasks linked to this listing's rows (any status) whose
        task is still live (`open`/`answered`): the ones to re-check after a sync."""
        live = exists(
            select(HumanTask.id).where(
                HumanTask.tenant_id == ListingIssue.tenant_id,
                HumanTask.id == ListingIssue.human_task_id,
                HumanTask.status.in_(("open", "answered")),
            )
        )
        stmt = (
            self._select()
            .where(
                ListingIssue.listing_id == listing_id,
                ListingIssue.human_task_id.is_not(None),
                live,
            )
            .with_only_columns(ListingIssue.human_task_id)
            .distinct()
            .order_by(ListingIssue.human_task_id)
        )
        return [row for row in (await self.db.execute(stmt)).scalars() if row is not None]

    async def task_link_counts(self, task_id: uuid.UUID) -> tuple[int, int]:
        """SI4 A12: (rows linked to the task, of them still open), across every listing."""
        stmt = (
            self._select()
            .where(ListingIssue.human_task_id == task_id)
            .with_only_columns(
                func.count(ListingIssue.id),
                func.count(ListingIssue.id).filter(ListingIssue.status == OPEN),
            )
        )
        total, still_open = (await self.db.execute(stmt)).one()
        return int(total), int(still_open)

    async def unlinked(self, issue_ids: Sequence[uuid.UUID]) -> builtins.list[uuid.UUID]:
        """SI11: of these rows, the open ones no human task stands behind, oldest first."""
        if not issue_ids:
            return []
        stmt = (
            self._select()
            .where(
                ListingIssue.id.in_(tuple(issue_ids)),
                ListingIssue.status == OPEN,
                ListingIssue.human_task_id.is_(None),
            )
            .with_only_columns(ListingIssue.id)
            .order_by(ListingIssue.created_at, ListingIssue.id)
        )
        return builtins.list((await self.db.execute(stmt)).scalars())

    async def link_ids(self, issue_ids: Sequence[uuid.UUID], task_id: uuid.UUID) -> int:
        """Link open issues, by id, to a human task."""
        self._own_session()
        if not issue_ids:
            return 0
        criterion = self._tenant_criterion(include_platform=False)
        stmt = self._scoped_write(
            update(ListingIssue)
            .where(criterion, ListingIssue.id.in_(tuple(issue_ids)), ListingIssue.status == OPEN)
            .values(human_task_id=task_id)
        ).returning(ListingIssue.id)
        return len((await self._run(stmt)).all())

    async def upsert_open(self, new: NewIssue) -> tuple[ListingIssue, bool]:
        self._own_session()
        previous = await self.latest_closed(new.listing_id, new.dedupe_key)
        values: dict[str, Any] = {
            "tenant_id": self.tenant_id,
            "listing_id": new.listing_id,
            "marketplace": new.marketplace,
            "listing_item_id": new.listing_item_id,
            "listing_version_id": new.listing_version_id,
            "validation_run_id": new.validation_run_id,
            "last_validation_run_id": new.validation_run_id,
            "job_id": new.job_id,
            "source": new.source,
            "field_path": new.field_path,
            "error_code": new.error_code,
            "error_class": new.error_class,
            "retry_class": new.retry_class,
            "severity": new.severity,
            "auto_fixable": new.auto_fixable,
            "fix_action": new.fix_action,
            "rule_id": new.rule_id,
            "marketplace_code": new.marketplace_code,
            "message": new.message,
            "suggestion": new.suggestion,
            "value_submitted": new.value_submitted,
            "dedupe_key": new.dedupe_key,
            "recurrence_of_id": None if previous is None else previous.id,
        }
        insert = pg_insert(ListingIssue).values(**values)
        stmt: Any = insert.on_conflict_do_update(
            index_elements=[ListingIssue.listing_id, ListingIssue.dedupe_key],
            # a literal predicate: a bound one stops Postgres inferring `li_one_open` once psycopg
            # prepares the statement (from the 11th run on one connection)
            index_where=text("status = 'open'"),
            set_={
                "occurrences": ListingIssue.occurrences + 1,
                "last_seen_at": func.now(),
                "last_validation_run_id": insert.excluded.last_validation_run_id,
                "message": insert.excluded.message,
                "value_submitted": insert.excluded.value_submitted,
            },
        ).returning(ListingIssue, literal_column("(xmax = 0)").label("inserted"))
        row = (await self._run(stmt)).one()
        return row[0], bool(row[1])

    async def close_missing(
        self,
        listing_id: uuid.UUID,
        source: str,
        keep_keys: Sequence[str],
        resolution: str,
        resolver: uuid.UUID | None = None,
        *,
        field_paths: Sequence[str] | None = None,
        channel: Literal["live", "preview"] = "live",
    ) -> int:
        """Close this listing's open rows of `source` whose key is absent (module docstring).
        SI11 (CP-D4): only rows of `channel`: a preview row (key ending `|preview`) closes only by
        a later preview, a live row never by a preview."""
        self._own_session()
        if field_paths is not None and not field_paths:
            return 0
        criterion = self._tenant_criterion(include_platform=False)
        status = closed_status(resolution)
        stmt = update(ListingIssue).where(
            criterion,
            ListingIssue.listing_id == listing_id,
            ListingIssue.source == source,
            ListingIssue.status == OPEN,
        )
        if keep_keys:
            stmt = stmt.where(ListingIssue.dedupe_key.not_in(list(keep_keys)))
        if field_paths is not None:
            stmt = stmt.where(ListingIssue.field_path.in_(list(field_paths)))
        is_preview = ListingIssue.dedupe_key.like(PREVIEW_KEY_LIKE)
        stmt = stmt.where(is_preview if channel == "preview" else not_(is_preview))
        stmt = self._scoped_write(
            stmt.values(
                status=status,
                resolution=resolution,
                resolved_at=func.now(),
                resolved_by_user_id=resolver,
            )
        ).returning(ListingIssue.id)
        return len((await self._run(stmt)).all())

    async def open_row(self, listing_id: uuid.UUID, key: str) -> ListingIssue | None:
        """The listing's open row with this key (`li_one_open`: at most one)."""
        stmt = self._select().where(
            ListingIssue.listing_id == listing_id,
            ListingIssue.dedupe_key == key,
            ListingIssue.status == OPEN,
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def close_open(self, issue_id: uuid.UUID, resolution: str) -> bool:
        """The compare-and-set that closes one open row with a system resolution (no user), in
        the status `closed_status` names. False for a closed row or another tenant's id."""
        self._own_session()
        criterion = self._tenant_criterion(include_platform=False)
        stmt = self._scoped_write(
            update(ListingIssue)
            .where(criterion, ListingIssue.id == issue_id, ListingIssue.status == OPEN)
            .values(
                status=closed_status(resolution),
                resolution=resolution,
                resolved_at=func.now(),
                resolved_by_user_id=None,
            )
        ).returning(ListingIssue.id)
        return len((await self._run(stmt)).all()) == 1

    async def close_auto_fixed(self, issue_id: uuid.UUID, action_taken: str) -> bool:
        """SI4 A10 (CP-D2): the compare-and-set that closes one open row the fix pass fixed,
        `auto_fixed` / `deterministic_fix` with the trail text; the system resolves it (no user).
        False for a closed row or another tenant's id (0 rows)."""
        self._own_session()
        criterion = self._tenant_criterion(include_platform=False)
        stmt = self._scoped_write(
            update(ListingIssue)
            .where(criterion, ListingIssue.id == issue_id, ListingIssue.status == OPEN)
            .values(
                status="auto_fixed",
                resolution="deterministic_fix",
                action_taken=action_taken,
                resolved_at=func.now(),
                resolved_by_user_id=None,
            )
        ).returning(ListingIssue.id)
        return len((await self._run(stmt)).all()) == 1

    async def page(
        self,
        filters: IssueFilter,
        *,
        after: tuple[datetime, uuid.UUID] | None,
        limit: int,
    ) -> tuple[builtins.list[ListingIssue], bool]:
        if isinstance(limit, bool) or not 1 <= limit <= PAGE_LIMIT_MAX:
            raise ValueError(f"limit must be in 1..{PAGE_LIMIT_MAX}")
        # an issue of an archived listing (its product was deleted) leaves the inbox
        archived = exists(
            select(Listing.id).where(
                Listing.tenant_id == ListingIssue.tenant_id,
                Listing.id == ListingIssue.listing_id,
                Listing.archived_at.is_not(None),
            )
        )
        stmt = self._select().where(ListingIssue.status == filters.status, ~archived)
        if filters.listing_id is not None:
            stmt = stmt.where(ListingIssue.listing_id == filters.listing_id)
        if filters.source is not None:
            stmt = stmt.where(ListingIssue.source == filters.source)
        if filters.error_code is not None:
            stmt = stmt.where(ListingIssue.error_code == filters.error_code)
        if filters.marketplace is not None:
            stmt = stmt.where(ListingIssue.marketplace == filters.marketplace)
        if after is not None:
            at, row_id = after
            bound = tuple_(
                literal(at, ListingIssue.created_at.type), literal(row_id, ListingIssue.id.type)
            )
            stmt = stmt.where(tuple_(ListingIssue.created_at, ListingIssue.id) < bound)
        stmt = stmt.order_by(ListingIssue.created_at.desc(), ListingIssue.id.desc())
        rows = builtins.list((await self.db.execute(stmt.limit(limit + 1))).scalars())
        return rows[:limit], len(rows) > limit

    async def open_ids_for_listings(
        self, listing_ids: Sequence[uuid.UUID]
    ) -> builtins.list[uuid.UUID]:
        """The open rows of these listings, in id order (product delete: the audit names them;
        they stay open with their archived listing and leave the inbox)."""
        if not listing_ids:
            return []
        stmt = (
            self._select()
            .where(ListingIssue.listing_id.in_(list(listing_ids)), ListingIssue.status == OPEN)
            .with_only_columns(ListingIssue.id)
            .order_by(ListingIssue.id)
        )
        return builtins.list((await self.db.execute(stmt)).scalars())

    async def get(self, issue_id: uuid.UUID) -> ListingIssue | None:
        """SI13: one row of this tenant (the diagnosis job's sample), unlocked."""
        stmt = self._select().where(ListingIssue.id == issue_id)
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def lock(self, issue_id: uuid.UUID) -> ListingIssue | None:
        stmt = (
            self._select()
            .where(ListingIssue.id == issue_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def dismiss(self, issue_id: uuid.UUID, user_id: uuid.UUID) -> ListingIssue | None:
        """The compare-and-set: only an open warning matches; None otherwise."""
        self._own_session()
        criterion = self._tenant_criterion(include_platform=False)
        stmt = self._scoped_write(
            update(ListingIssue)
            .where(
                criterion,
                ListingIssue.id == issue_id,
                ListingIssue.status == OPEN,
                ListingIssue.severity == "warning",
            )
            .values(
                status="dismissed",
                resolution="accepted_warning",
                resolved_at=func.now(),
                resolved_by_user_id=user_id,
            )
        ).returning(ListingIssue)
        found: ListingIssue | None = (await self._run(stmt)).scalar_one_or_none()
        return found
