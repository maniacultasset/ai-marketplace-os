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
from typing import Any, Final

from sqlalchemy import func, literal, literal_column, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db.guard import TENANT_INFO_KEY, CrossTenantQueryError
from app.db.integrity import map_db_error, to_conflict
from app.db.models.issues import ListingIssue
from app.repositories.base import TenantScopedRepository

PAGE_LIMIT_MAX: Final = 200
OPEN: Final = "open"


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
            index_where=ListingIssue.status == OPEN,
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
    ) -> int:
        """Close this listing's open rows of `source` whose key is absent (module docstring)."""
        self._own_session()
        if field_paths is not None and not field_paths:
            return 0
        criterion = self._tenant_criterion(include_platform=False)
        status = "auto_fixed" if resolution == "deterministic_fix" else "resolved"
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
        stmt = self._scoped_write(
            stmt.values(
                status=status,
                resolution=resolution,
                resolved_at=func.now(),
                resolved_by_user_id=resolver,
            )
        ).returning(ListingIssue.id)
        return len((await self._run(stmt)).all())

    async def page(
        self,
        filters: IssueFilter,
        *,
        after: tuple[datetime, uuid.UUID] | None,
        limit: int,
    ) -> tuple[builtins.list[ListingIssue], bool]:
        if isinstance(limit, bool) or not 1 <= limit <= PAGE_LIMIT_MAX:
            raise ValueError(f"limit must be in 1..{PAGE_LIMIT_MAX}")
        stmt = self._select().where(ListingIssue.status == filters.status)
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
