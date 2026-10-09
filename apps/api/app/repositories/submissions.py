"""`listing_submissions`, `publish_field_results` and `listing_items` (S21 T6, T7; DATABASE.md
:834-887). Every statement is tenant-scoped (invariant 5); the 0004 guards (`ls_guard`,
`li_guard`) and the 0008 retry rule are the database backstops.

- `ListingSubmissionRepository`: the write-ahead `pending` row (`insert_pending`; a second open row
  of a listing is refused by `ux_ls_open` → 409 SUBMISSION_IN_FLIGHT), `mark_sent`, the fenced
  `resolve` (only a `pending` row, and only the given job's when `job_id` is passed), the sync's
  `accepted → …` move (`settle_accepted`), `supersede_open_accepted` (a newer version's attempt 1),
  `count_for_version`, `latest` and the keyset `page` (newest first by `(created_at, id)`).
- `PublishFieldResultRepository`: append-only field verdicts and the retry payload of DATABASE.md
  :887: `DISTINCT ON (listing_item_id, field)` newest first over the approved version's rows,
  **then** filtered to `failed` (filtering first would resend a field that later succeeded).
- `ListingItemRepository`: the items upserted with the pending row (`first_submitted_at` set once),
  per-item state writes and reads by listing.
"""

from __future__ import annotations

import builtins
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import func, literal, null, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db.guard import TENANT_INFO_KEY, CrossTenantQueryError
from app.db.integrity import map_db_error, to_conflict
from app.db.models.listings import ListingItem
from app.db.models.ops import ListingSubmission, PublishFieldResult
from app.repositories.base import TenantScopedRepository
from app.repositories.listings import db_values, job_actor

PAGE_LIMIT_MAX: Final = 200
TERMINAL_OUTCOMES: Final = frozenset(
    {"partially_accepted", "rejected", "error", "live", "superseded"}
)


@dataclass(frozen=True, slots=True)
class NewSubmission:
    """The columns of one write-ahead `pending` row (DATABASE.md:850-869)."""

    id: uuid.UUID
    listing_id: uuid.UUID
    listing_version_id: uuid.UUID
    marketplace: str
    marketplace_account_id: uuid.UUID
    job_id: uuid.UUID
    attempt: int
    operation: str
    payload_sha256: bytes
    payload_storage_key: str
    facts_digest: bytes
    idempotency_key: str
    requested_by_user_id: uuid.UUID
    field_paths: Sequence[str] | None = None
    retry_justification: str | None = None
    previous_submission_id: uuid.UUID | None = None


async def _run(db: Any, stmt: Any) -> Any:
    try:
        return await db.execute(stmt, execution_options={"populate_existing": True})
    except IntegrityError as exc:
        raise to_conflict(exc) from exc
    except DBAPIError as exc:
        mapped = map_db_error(exc)
        if mapped is not None:
            raise mapped from exc
        raise


class ListingSubmissionRepository(TenantScopedRepository[ListingSubmission]):
    model = ListingSubmission

    def _own_session(self) -> None:
        if self.db.info.get(TENANT_INFO_KEY) != self.tenant_id:
            raise CrossTenantQueryError("listing_submissions write for another tenant's session")

    async def insert_pending(self, new: NewSubmission) -> ListingSubmission:
        self._own_session()
        stmt = (
            pg_insert(ListingSubmission)
            .values(
                id=new.id,
                tenant_id=self.tenant_id,
                listing_id=new.listing_id,
                listing_version_id=new.listing_version_id,
                marketplace=new.marketplace,
                marketplace_account_id=new.marketplace_account_id,
                job_id=new.job_id,
                attempt=new.attempt,
                operation=new.operation,
                field_paths=None if new.field_paths is None else list(new.field_paths),
                payload_sha256=new.payload_sha256,
                payload_storage_key=new.payload_storage_key,
                facts_digest=new.facts_digest,
                idempotency_key=new.idempotency_key,
                retry_justification=new.retry_justification,
                previous_submission_id=new.previous_submission_id,
                requested_by_user_id=new.requested_by_user_id,
                actor_type="user",
            )
            .returning(ListingSubmission)
        )
        row: ListingSubmission = (await _run(self.db, stmt)).scalar_one()
        return row

    async def get(self, submission_id: uuid.UUID) -> ListingSubmission | None:
        return await self.get_by_id(submission_id)

    async def latest(self, listing_id: uuid.UUID) -> ListingSubmission | None:
        stmt = (
            self._select()
            .where(ListingSubmission.listing_id == listing_id)
            .order_by(ListingSubmission.created_at.desc(), ListingSubmission.attempt.desc())
            .limit(1)
            .execution_options(populate_existing=True)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def open_for_listing(self, listing_id: uuid.UUID) -> ListingSubmission | None:
        stmt = self._select().where(
            ListingSubmission.listing_id == listing_id,
            ListingSubmission.outcome.in_(("pending", "accepted")),
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def count_for_version(self, listing_id: uuid.UUID, version_id: uuid.UUID) -> int:
        stmt = (
            self._select()
            .where(
                ListingSubmission.listing_id == listing_id,
                ListingSubmission.listing_version_id == version_id,
            )
            .with_only_columns(func.count())
        )
        return int((await self.db.execute(stmt)).scalar_one())

    async def has_outcome(
        self, listing_id: uuid.UUID, version_id: uuid.UUID, outcomes: Sequence[str]
    ) -> bool:
        stmt = (
            self._select()
            .where(
                ListingSubmission.listing_id == listing_id,
                ListingSubmission.listing_version_id == version_id,
                ListingSubmission.outcome.in_(list(outcomes)),
            )
            .with_only_columns(ListingSubmission.id)
            .limit(1)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none() is not None

    async def listing_has_outcome(self, listing_id: uuid.UUID, outcomes: Sequence[str]) -> bool:
        """`has_outcome` over every version of the listing: whether any attempt of it ever
        reached the marketplace (an older version's partial makes the next version an update)."""
        stmt = (
            self._select()
            .where(
                ListingSubmission.listing_id == listing_id,
                ListingSubmission.outcome.in_(list(outcomes)),
            )
            .with_only_columns(ListingSubmission.id)
            .limit(1)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none() is not None

    async def last_for_version(
        self, listing_id: uuid.UUID, version_id: uuid.UUID
    ) -> ListingSubmission | None:
        stmt = (
            self._select()
            .where(
                ListingSubmission.listing_id == listing_id,
                ListingSubmission.listing_version_id == version_id,
            )
            .order_by(ListingSubmission.attempt.desc())
            .limit(1)
            .execution_options(populate_existing=True)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    def _write(self, *where: Any) -> Any:
        criterion = self._tenant_criterion(include_platform=False)
        return self._scoped_write(update(ListingSubmission).where(criterion, *where))

    async def mark_sent(self, submission_id: uuid.UUID) -> bool:
        self._own_session()
        stmt = (
            self._write(
                ListingSubmission.id == submission_id, ListingSubmission.outcome == "pending"
            )
            .values(sent_at=func.now())
            .returning(ListingSubmission.id)
        )
        return (await _run(self.db, stmt)).scalar_one_or_none() is not None

    async def resolve(
        self,
        submission_id: uuid.UUID,
        *,
        outcome: str,
        job_id: uuid.UUID | None = None,
        error_code: str | None = None,
        report: Mapping[str, Any] | None = None,
        submission_ref: str | None = None,
        raw_key: str | None = None,
        raw_sha: bytes | None = None,
    ) -> bool:
        """`pending → outcome`, fenced on the row still `pending` (and on `job_id` when given):
        a stale job's write changes 0 rows and returns False."""
        self._own_session()
        where = [ListingSubmission.id == submission_id, ListingSubmission.outcome == "pending"]
        if job_id is not None:
            where.append(ListingSubmission.job_id == job_id)
        values: dict[str, Any] = {
            "outcome": outcome,
            "error_code": error_code,
            "error_report_json": null() if report is None else dict(report),
            "responded_at": func.now(),
            "resolved_at": func.now() if outcome in TERMINAL_OUTCOMES else None,
        }
        if submission_ref is not None:
            values["submission_ref"] = submission_ref[:128]
        if raw_key is not None:
            values["raw_response_storage_key"] = raw_key
            values["raw_response_sha256"] = raw_sha
        stmt = self._write(*where).values(**values).returning(ListingSubmission.id)
        return (await _run(self.db, stmt)).scalar_one_or_none() is not None

    async def settle_accepted(
        self,
        submission_id: uuid.UUID,
        *,
        outcome: str,
        error_code: str | None = None,
        report: Mapping[str, Any] | None = None,
    ) -> bool:
        """The status sync's `accepted → live | partially_accepted | rejected | error` (the last
        only as MARKETPLACE_UNCONFIRMED, `ls_guard`)."""
        self._own_session()
        stmt = (
            self._write(
                ListingSubmission.id == submission_id, ListingSubmission.outcome == "accepted"
            )
            .values(
                outcome=outcome,
                error_code=error_code,
                error_report_json=null() if report is None else dict(report),
                resolved_at=func.now(),
            )
            .returning(ListingSubmission.id)
        )
        return (await _run(self.db, stmt)).scalar_one_or_none() is not None

    async def supersede_open_accepted(self, listing_id: uuid.UUID, version_id: uuid.UUID) -> int:
        """An older version's open `accepted` row → `superseded` (DATABASE.md:872); run in the
        transaction that inserts the newer version's attempt 1."""
        self._own_session()
        stmt = (
            self._write(
                ListingSubmission.listing_id == listing_id,
                ListingSubmission.outcome == "accepted",
                ListingSubmission.listing_version_id != version_id,
            )
            .values(outcome="superseded", resolved_at=func.now())
            .returning(ListingSubmission.id)
        )
        return len((await _run(self.db, stmt)).all())

    async def page(
        self,
        listing_id: uuid.UUID,
        *,
        after: tuple[datetime, uuid.UUID] | None,
        limit: int,
    ) -> tuple[builtins.list[ListingSubmission], bool]:
        if isinstance(limit, bool) or not 1 <= limit <= PAGE_LIMIT_MAX:
            raise ValueError(f"limit must be in 1..{PAGE_LIMIT_MAX}")
        s = ListingSubmission
        stmt = self._select().where(s.listing_id == listing_id)
        if after is not None:
            at, row_id = after
            bound = tuple_(literal(at, s.created_at.type), literal(row_id, s.id.type))
            stmt = stmt.where(tuple_(s.created_at, s.id) < bound)
        stmt = stmt.order_by(s.created_at.desc(), s.id.desc()).limit(limit + 1)
        rows = builtins.list((await self.db.execute(stmt)).scalars())
        return rows[:limit], len(rows) > limit


@dataclass(frozen=True, slots=True)
class NewFieldResult:
    submission_id: uuid.UUID
    listing_id: uuid.UUID
    listing_version_id: uuid.UUID
    listing_item_id: uuid.UUID
    field: str
    status: str
    error_json: Mapping[str, Any] | None = None
    marketplace_code: str | None = None


class PublishFieldResultRepository(TenantScopedRepository[PublishFieldResult]):
    model = PublishFieldResult

    async def insert_many(self, rows: Sequence[NewFieldResult]) -> int:
        if not rows:
            return 0
        if self.db.info.get(TENANT_INFO_KEY) != self.tenant_id:
            raise CrossTenantQueryError("publish_field_results write for another tenant")
        stmt = pg_insert(PublishFieldResult).values(
            [
                {
                    "tenant_id": self.tenant_id,
                    "submission_id": r.submission_id,
                    "listing_id": r.listing_id,
                    "listing_version_id": r.listing_version_id,
                    "listing_item_id": r.listing_item_id,
                    "field": r.field,
                    "status": r.status,
                    "error_json": null() if r.error_json is None else dict(r.error_json),
                    "marketplace_code": (
                        None if r.marketplace_code is None else r.marketplace_code[:128]
                    ),
                }
                for r in rows
            ]
        )
        await _run(self.db, stmt)
        return len(rows)

    async def retry_payload(
        self, listing_id: uuid.UUID, version_id: uuid.UUID
    ) -> dict[str, tuple[str, ...]]:
        """{seller_sku: failed paths}: the newest verdict per (item, field) of the version, then
        only the failed ones (DATABASE.md:887)."""
        r = PublishFieldResult
        latest = (
            self._select()
            .where(r.listing_id == listing_id, r.listing_version_id == version_id)
            .with_only_columns(r.listing_item_id, r.field, r.status)
            .distinct(r.listing_item_id, r.field)
            .order_by(r.listing_item_id, r.field, r.created_at.desc(), r.id.desc())
            .subquery()
        )
        stmt = self._scoped_outer(
            select(latest.c.listing_item_id, latest.c.field)
            .where(latest.c.status == "failed")
            .order_by(latest.c.listing_item_id, latest.c.field),
            latest,
        )
        failed: dict[uuid.UUID, builtins.list[str]] = {}
        for item_id, path in (await self.db.execute(stmt)).all():
            failed.setdefault(item_id, []).append(path)
        if not failed:
            return {}
        skus = {
            row.id: row.seller_sku
            for row in await ListingItemRepository(self.db, self.tenant_id).for_listing(listing_id)
        }
        return {skus[i]: tuple(sorted(paths)) for i, paths in failed.items() if i in skus}

    async def for_submissions(
        self, submission_ids: Sequence[uuid.UUID]
    ) -> builtins.list[PublishFieldResult]:
        if not submission_ids:
            return []
        stmt = (
            self._select()
            .where(PublishFieldResult.submission_id.in_(list(submission_ids)))
            .order_by(PublishFieldResult.listing_item_id, PublishFieldResult.field)
        )
        return builtins.list((await self.db.execute(stmt)).scalars())


@dataclass(frozen=True, slots=True)
class NewItem:
    product_id: uuid.UUID
    item_role: str
    seller_sku: str


class ListingItemRepository(TenantScopedRepository[ListingItem]):
    model = ListingItem

    async def upsert_for_submission(
        self, listing_id: uuid.UUID, marketplace: str, items: Sequence[NewItem]
    ) -> dict[str, ListingItem]:
        """Insert the listing's items (`li_guard` checks role, product and SKU) or keep the
        existing rows; `first_submitted_at` is set once. Returns {seller_sku: row}."""
        if self.db.info.get(TENANT_INFO_KEY) != self.tenant_id:
            raise CrossTenantQueryError("listing_items write for another tenant's session")
        for one in items:
            stmt = (
                pg_insert(ListingItem)
                .values(
                    tenant_id=self.tenant_id,
                    listing_id=listing_id,
                    marketplace=marketplace,
                    product_id=one.product_id,
                    item_role=one.item_role,
                    seller_sku=one.seller_sku,
                    first_submitted_at=func.now(),
                )
                .on_conflict_do_update(
                    index_elements=[ListingItem.listing_id, ListingItem.product_id],
                    set_={
                        "first_submitted_at": func.coalesce(
                            ListingItem.first_submitted_at, func.now()
                        )
                    },
                )
            )
            await _run(self.db, stmt)
        return {row.seller_sku: row for row in await self.for_listing(listing_id)}

    async def for_listing(self, listing_id: uuid.UUID) -> builtins.list[ListingItem]:
        stmt = (
            self._select()
            .where(ListingItem.listing_id == listing_id)
            .order_by(ListingItem.seller_sku)
            .execution_options(populate_existing=True)
        )
        return builtins.list((await self.db.execute(stmt)).scalars())

    async def write_state(
        self, item_id: uuid.UUID, *, job_id: uuid.UUID | None = None, **values: Any
    ) -> None:
        """One item's state columns (`marketplace_state`, `external_id`, `live_*`,
        `last_synced_at`); `state_changed_at` follows a state change. `live` needs the job GUC
        (`li_guard`): the caller binds it."""
        allowed = {
            "marketplace_state",
            "external_id",
            "live_url",
            "live_version_id",
            "live_confirmed_at",
            "last_synced_at",
        }
        if not values or set(values) - allowed:
            raise ValueError("only the item state columns are written here")
        criterion = self._tenant_criterion(include_platform=False)
        values = db_values(values)
        if "marketplace_state" in values:
            values["state_changed_at"] = func.now()
        stmt = self._scoped_write(
            update(ListingItem).where(criterion, ListingItem.id == item_id).values(**values)
        )
        async with job_actor(self.db, job_id):  # `li_guard`: `live` only from a job
            await _run(self.db, stmt)
