"""`listings` and `listing_versions` (DATABASE.md:740-789, §6.1 :1376-1402; S18 T3, T5).

- `ListingRepository`: reads (plain or `FOR UPDATE`), `create` (status `created`, event `create`;
  a concurrent creator of the same `(product_id, marketplace)` gets 409 `LISTING_BUSY`), the
  newest `listing_generate` job of a listing, and `move`: the one writer of `status`. A move
  always sets `last_event` and `status_reason` explicitly (DATABASE.md:1402). A `UserActor` move
  needs the session's user GUC to be that user; a `JobActor` move binds the job GUC for its
  statement only (`bind_job_actor`). The guard's refusals come back as their 409s
  (`LST01` → `INVALID_STATUS_TRANSITION`, `LST02` → `LISTING_PAUSED|LISTING_ARCHIVED`).
- `ListingVersionRepository`: the next version number (call it under the listing lock), the
  INSERT of an immutable version, and reads scoped to one listing.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import JobKind
from sqlalchemy import func, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.core.errors import ConflictError
from app.db.guard import TENANT_INFO_KEY, CrossTenantQueryError
from app.db.integrity import map_db_error, to_conflict
from app.db.job_context import bind_job_actor, unbind_job_actor
from app.db.models.listings import Listing, ListingVersion
from app.db.models.ops import JobRun
from app.db.session import USER_INFO_KEY
from app.repositories.base import TenantScopedRepository

__all__ = [
    "JobActor",
    "ListingRepository",
    "ListingVersionRepository",
    "NewVersion",
    "UserActor",
]


@dataclass(frozen=True, slots=True)
class UserActor:
    user_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class JobActor:
    """A worker move: only the generation worker, its persist and the job hooks build one (a meta
    test pins the call sites)."""

    job_id: uuid.UUID


Actor = UserActor | JobActor


class _Jobs(TenantScopedRepository[JobRun]):
    model = JobRun


class ListingRepository(TenantScopedRepository[Listing]):
    model = Listing

    def _own_session(self) -> None:
        if self.db.info.get(TENANT_INFO_KEY) != self.tenant_id:
            raise CrossTenantQueryError("listings write for another tenant's unit of work")

    async def get(self, listing_id: uuid.UUID) -> Listing | None:
        return await self.get_by_id(listing_id)

    async def get_for_update(self, listing_id: uuid.UUID) -> Listing | None:
        stmt = (
            self._select()
            .where(Listing.id == listing_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def get_by_pair_for_update(
        self, product_id: uuid.UUID, marketplace: str
    ) -> Listing | None:
        stmt = (
            self._select()
            .where(Listing.product_id == product_id, Listing.marketplace == marketplace)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def create(self, product_id: uuid.UUID, marketplace: str, category: str) -> Listing:
        """A `created` listing; a concurrent creator of the pair waits for the winner and then
        gets 409 `LISTING_BUSY` (ON CONFLICT DO NOTHING, so the transaction stays usable)."""
        self._own_session()
        stmt = (
            pg_insert(Listing)
            .values(
                tenant_id=self.tenant_id,
                product_id=product_id,
                marketplace=marketplace,
                category=category,
            )
            .on_conflict_do_nothing(index_elements=[Listing.product_id, Listing.marketplace])
            .returning(Listing.id)
        )
        try:
            new_id = (await self.db.execute(stmt)).scalar_one_or_none()
        except IntegrityError as exc:
            raise to_conflict(exc) from exc
        except DBAPIError as exc:
            mapped = map_db_error(exc)
            if mapped is not None:
                raise mapped from exc
            raise
        if new_id is None:
            raise ConflictError(ErrorCode.LISTING_BUSY, "uq_listings_product_id_marketplace")
        created = await self.get_for_update(new_id)
        if created is None:  # unreachable: our own row in our own transaction
            raise ConflictError(ErrorCode.LISTING_BUSY, "uq_listings_product_id_marketplace")
        return created

    async def latest_generate_job_id(self, listing_id: uuid.UUID) -> uuid.UUID | None:
        jobs = _Jobs(self.db, self.tenant_id)
        stmt = (
            jobs._select()
            .where(
                JobRun.listing_id == listing_id,
                JobRun.kind == JobKind.LISTING_GENERATE.value,
            )
            .order_by(JobRun.queued_at.desc(), JobRun.id.desc())
            .limit(1)
            .with_only_columns(JobRun.id)
        )
        found: uuid.UUID | None = (await self.db.execute(stmt)).scalar_one_or_none()
        return found

    async def move(
        self,
        listing: Listing,
        *,
        to: str,
        event: str,
        actor: Actor,
        reason: str | None = None,
        current_version_id: uuid.UUID | None = None,
        clear_approval: bool = False,
    ) -> Listing:
        """The one status writer (module docstring). Returns the updated row."""
        self._own_session()
        values: dict[str, Any] = {"status": to, "status_reason": reason, "last_event": event}
        if current_version_id is not None:
            values["current_version_id"] = current_version_id
        if clear_approval:
            values["approved_version_id"] = None
        if isinstance(actor, UserActor):
            if self.db.info.get(USER_INFO_KEY) != actor.user_id:
                raise ValueError("a user move needs that user's unit of work")
        elif not isinstance(actor, JobActor):
            raise TypeError("actor must be a UserActor or a JobActor")
        criterion = self._tenant_criterion(include_platform=False)
        stmt = self._scoped_write(
            update(Listing).where(Listing.id == listing.id, criterion).values(**values)
        ).returning(Listing)
        try:
            if isinstance(actor, JobActor):
                await bind_job_actor(self.db, actor.job_id)
            row = (
                await self.db.execute(stmt, execution_options={"populate_existing": True})
            ).scalar_one_or_none()
            # a refused statement aborts the transaction, so the GUC dies with it; only a
            # successful move clears it for the statements that follow
            if isinstance(actor, JobActor):
                await unbind_job_actor(self.db)
        except DBAPIError as exc:
            mapped = map_db_error(exc)
            if mapped is not None:
                raise mapped from exc
            raise
        if row is None:
            raise ConflictError(ErrorCode.INVALID_STATUS_TRANSITION, "listings")
        return row


@dataclass(frozen=True, slots=True)
class NewVersion:
    """The columns of one `listing_versions` INSERT (DATABASE.md:779-785)."""

    listing_id: uuid.UUID
    version: int
    content_json: dict[str, Any]
    generated_by_user_id: uuid.UUID
    origin: str
    locale: str = "en-IN"
    image_reports_json: Sequence[Any] = ()
    edited_by_user_id: uuid.UUID | None = None
    parent_version_id: uuid.UUID | None = None
    changed_paths: Sequence[str] | None = None
    rules_version: int | None = None
    category_template_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    attempts_used: int = 0
    id: uuid.UUID | None = None


class ListingVersionRepository(TenantScopedRepository[ListingVersion]):
    model = ListingVersion

    async def next_version(self, listing_id: uuid.UUID) -> int:
        stmt = (
            self._select()
            .where(ListingVersion.listing_id == listing_id)
            .with_only_columns(func.coalesce(func.max(ListingVersion.version), 0))
        )
        return int((await self.db.execute(stmt)).scalar_one()) + 1

    async def insert(self, new: NewVersion) -> ListingVersion:
        """A Core INSERT of the writable columns only: `app_rw` holds no INSERT on the two
        approval columns (lv_guard privileges, DATABASE.md:789)."""
        if self.db.info.get(TENANT_INFO_KEY) != self.tenant_id:
            raise CrossTenantQueryError("listing_versions write for another tenant's unit of work")
        stmt = (
            pg_insert(ListingVersion)
            .values(
                id=new.id or uuid.uuid4(),
                tenant_id=self.tenant_id,
                listing_id=new.listing_id,
                version=new.version,
                content_json=new.content_json,
                image_reports_json=list(new.image_reports_json),
                generated_by_user_id=new.generated_by_user_id,
                edited_by_user_id=new.edited_by_user_id,
                origin=new.origin,
                parent_version_id=new.parent_version_id,
                changed_paths=None if new.changed_paths is None else list(new.changed_paths),
                locale=new.locale,
                rules_version=new.rules_version,
                category_template_id=new.category_template_id,
                job_id=new.job_id,
                attempts_used=new.attempts_used,
            )
            .returning(ListingVersion)
        )
        try:
            row = (
                await self.db.execute(stmt, execution_options={"populate_existing": True})
            ).scalar_one()
        except IntegrityError as exc:
            raise to_conflict(exc) from exc
        except DBAPIError as exc:
            mapped = map_db_error(exc)
            if mapped is not None:
                raise mapped from exc
            raise
        return row

    async def get(self, listing_id: uuid.UUID, version_id: uuid.UUID) -> ListingVersion | None:
        stmt = self._select().where(
            ListingVersion.listing_id == listing_id, ListingVersion.id == version_id
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def current(self, listing: Listing) -> ListingVersion | None:
        if listing.current_version_id is None:
            return None
        return await self.get(listing.id, listing.current_version_id)
