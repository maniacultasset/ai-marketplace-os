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

S19 adds `ListingRepository.lock_many(ids)` (every target listing `FOR UPDATE` in one statement,
in id order, so two overlapping approve batches cannot deadlock, DATABASE.md §7.1) and
`page(filters, after=, limit=)`: the review queue (`validated` or `needs_review`, not paused or
archived) oldest first by `(status_changed_at, id)`, any other filter newest first by
`(last_event_at, id)`.

`ListingRepository.preview_source` (S20 T18) reads what a marketplace adapter needs to know about
one listing, SELECT only and under the tenant's RLS session: the listing, its current version,
its product, the product's live children (sorted by SKU, a family's send order) and the template
the version pinned (`listing_versions.category_template_id`, current or not). Each table is read
through its own tenant-scoped repository; the template is platform-global. Another tenant's
listing id is `None`, exactly as an unknown one.
"""

from __future__ import annotations

import builtins
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import JobKind
from sqlalchemy import func, literal, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.core.errors import ConflictError
from app.db.guard import TENANT_INFO_KEY, CrossTenantQueryError
from app.db.integrity import map_db_error, to_conflict
from app.db.job_context import bind_job_actor, unbind_job_actor
from app.db.models.catalog import Product
from app.db.models.listings import Listing, ListingVersion
from app.db.models.ops import JobRun
from app.db.session import USER_INFO_KEY
from app.repositories.base import TenantScopedRepository
from app.repositories.templates import CategoryTemplateRepository, PinnedTemplate

__all__ = [
    "QUEUE_STATUSES",
    "JobActor",
    "ListingFilter",
    "ListingRepository",
    "ListingVersionRepository",
    "NewVersion",
    "PreviewSource",
    "SourceProduct",
    "SourceVersion",
    "UserActor",
]


QUEUE_STATUSES: Final = frozenset({"validated", "needs_review"})
PAGE_LIMIT_MAX: Final = 200


@dataclass(frozen=True, slots=True)
class ListingFilter:
    status: str | None = None
    marketplace: str | None = None
    product_id: uuid.UUID | None = None

    @property
    def queue(self) -> bool:
        """The review queue: oldest first by `status_changed_at` (BACKEND-ARCHITECTURE §3.5)."""
        return self.status in QUEUE_STATUSES


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


@dataclass(frozen=True, slots=True)
class SourceVersion:
    id: uuid.UUID
    version: int
    content: Mapping[str, Any]
    category_template_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class SourceProduct:
    id: uuid.UUID
    sku: str
    variation_theme: str | None
    attributes: Mapping[str, Any] = field(default_factory=dict)
    variant_attributes: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PreviewSource:
    listing_id: uuid.UUID
    marketplace: str
    category: str
    status: str
    product: SourceProduct
    version: SourceVersion | None
    """None while the listing has no current version."""
    children: tuple[SourceProduct, ...] = ()
    template: PinnedTemplate | None = None
    """None when the version pinned no template, or a template with no `product_type`."""


class _Versions(TenantScopedRepository[ListingVersion]):
    model = ListingVersion


class _Products(TenantScopedRepository[Product]):
    model = Product


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

    async def lock_many(self, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, Listing]:
        """Every listing of this tenant among `ids`, locked `FOR UPDATE` in id order in one
        statement; unknown and other tenants' ids are absent."""
        wanted = sorted({_uuid(i) for i in ids})
        if not wanted:
            return {}
        stmt = (
            self._select()
            .where(Listing.id.in_(wanted))
            .order_by(Listing.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return {row.id: row for row in (await self.db.execute(stmt)).scalars()}

    async def page(
        self,
        filters: ListingFilter,
        *,
        after: tuple[datetime, uuid.UUID] | None,
        limit: int,
    ) -> tuple[builtins.list[Listing], bool]:
        """One keyset page (module docstring) and whether more rows follow."""
        if isinstance(limit, bool) or not 1 <= limit <= PAGE_LIMIT_MAX:
            raise ValueError(f"limit must be in 1..{PAGE_LIMIT_MAX}")
        stmt = self._select()
        if filters.status is not None:
            stmt = stmt.where(Listing.status == filters.status)
        if filters.marketplace is not None:
            stmt = stmt.where(Listing.marketplace == filters.marketplace)
        if filters.product_id is not None:
            stmt = stmt.where(Listing.product_id == filters.product_id)
        if filters.queue:
            stmt = stmt.where(Listing.paused_at.is_(None), Listing.archived_at.is_(None))
            key = (Listing.status_changed_at, Listing.id)
        else:
            key = (Listing.last_event_at, Listing.id)
        if after is not None:
            at, row_id = after
            bound = tuple_(literal(at, key[0].type), literal(row_id, Listing.id.type))
            stmt = stmt.where(tuple_(*key) > bound if filters.queue else tuple_(*key) < bound)
        order = (key[0].asc(), key[1].asc()) if filters.queue else (key[0].desc(), key[1].desc())
        rows = builtins.list(
            (await self.db.execute(stmt.order_by(*order).limit(limit + 1))).scalars()
        )
        return rows[:limit], len(rows) > limit

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
        approved_version_id: uuid.UUID | None = None,
    ) -> Listing:
        """The one status writer (module docstring). Returns the updated row."""
        self._own_session()
        if clear_approval and approved_version_id is not None:
            raise ValueError("a move either clears the approval or sets it")
        values: dict[str, Any] = {"status": to, "status_reason": reason, "last_event": event}
        if current_version_id is not None:
            values["current_version_id"] = current_version_id
        if clear_approval:
            values["approved_version_id"] = None
        if approved_version_id is not None:
            values["approved_version_id"] = approved_version_id
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

    async def preview_source(self, listing_id: uuid.UUID) -> PreviewSource | None:
        lst = Listing
        row = (
            await self.db.execute(
                self._select()
                .where(lst.id == listing_id)
                .with_only_columns(
                    lst.id,
                    lst.marketplace,
                    lst.category,
                    lst.status,
                    lst.product_id,
                    lst.current_version_id,
                )
            )
        ).one_or_none()
        if row is None:
            return None
        products = _Products(self.db, self.tenant_id)
        product = await _product(products, row.product_id)
        if product is None:
            return None
        version = await self._version(row.id, row.current_version_id)
        template = None
        if version is not None and version.category_template_id is not None:
            pinned = await CategoryTemplateRepository(self.db).pinned(version.category_template_id)
            template = pinned if pinned is not None and pinned.product_type else None
        return PreviewSource(
            listing_id=row.id,
            marketplace=row.marketplace,
            category=row.category,
            status=row.status,
            product=product,
            version=version,
            children=await _children(products, product.id),
            template=template,
        )

    async def _version(
        self, listing_id: uuid.UUID, version_id: uuid.UUID | None
    ) -> SourceVersion | None:
        if version_id is None:
            return None
        v = ListingVersion
        repo = _Versions(self.db, self.tenant_id)
        row = (
            await self.db.execute(
                repo._select()
                .where(v.id == version_id, v.listing_id == listing_id)
                .with_only_columns(v.id, v.version, v.content_json, v.category_template_id)
            )
        ).one_or_none()
        if row is None:
            return None
        return SourceVersion(row.id, row.version, dict(row.content_json), row.category_template_id)


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

    async def get_many(self, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, ListingVersion]:
        """The versions among `ids` of this tenant, in one SELECT (S19: a batch's current
        versions)."""
        wanted = sorted({_uuid(i) for i in ids})
        if not wanted:
            return {}
        stmt = self._select().where(ListingVersion.id.in_(wanted))
        return {row.id: row for row in (await self.db.execute(stmt)).scalars()}

    async def chain_links(self, listing_id: uuid.UUID) -> builtins.list[Any]:
        """Every version of a listing with the columns the contributor chain walks (S19 T4)."""
        v = ListingVersion
        stmt = (
            self._select()
            .where(v.listing_id == listing_id)
            .with_only_columns(
                v.id,
                v.origin,
                v.parent_version_id,
                v.changed_paths,
                v.generated_by_user_id,
                v.edited_by_user_id,
            )
        )
        return builtins.list((await self.db.execute(stmt)).all())

    async def approve(
        self, listing_id: uuid.UUID, version_id: uuid.UUID, approver: uuid.UUID
    ) -> bool:
        """`UPDATE ... SET approved_by_user_id WHERE approved_by_user_id IS NULL` (DATABASE.md
        §7.1). `lv_guard` stamps `approved_at` and refuses a maker, an editor, an inactive or
        non-approving member or a stale version with MKC01 (→ `ConflictError`
        MAKER_CHECKER_VIOLATION). False when the version was already approved."""
        if self.db.info.get(TENANT_INFO_KEY) != self.tenant_id:
            raise CrossTenantQueryError("listing_versions write for another tenant's unit of work")
        criterion = self._tenant_criterion(include_platform=False)
        stmt = self._scoped_write(
            update(ListingVersion)
            .where(
                ListingVersion.id == version_id,
                ListingVersion.listing_id == listing_id,
                ListingVersion.approved_by_user_id.is_(None),
                criterion,
            )
            .values(approved_by_user_id=approver)
        ).returning(ListingVersion.id)
        try:
            row = (await self.db.execute(stmt)).scalar_one_or_none()
        except DBAPIError as exc:
            mapped = map_db_error(exc)
            if mapped is not None:
                raise mapped from exc
            raise
        return row is not None


def _uuid(value: object) -> uuid.UUID:
    if not isinstance(value, uuid.UUID):
        raise ValueError("listing ids must be UUIDs")
    return value


_PRODUCT_COLUMNS = (
    Product.id,
    Product.sku,
    Product.variation_theme,
    Product.attributes,
    Product.variant_attributes,
)


def _source(row: Any) -> SourceProduct:
    return SourceProduct(
        id=row.id,
        sku=row.sku,
        variation_theme=row.variation_theme,
        attributes=dict(row.attributes or {}),
        variant_attributes={str(k): str(v) for k, v in (row.variant_attributes or {}).items()},
    )


async def _product(repo: _Products, product_id: uuid.UUID) -> SourceProduct | None:
    stmt = (
        repo._select()
        .where(Product.id == product_id, Product.deleted_at.is_(None))
        .with_only_columns(*_PRODUCT_COLUMNS)
    )
    row = (await repo.db.execute(stmt)).one_or_none()
    return None if row is None else _source(row)


async def _children(repo: _Products, parent_id: uuid.UUID) -> tuple[SourceProduct, ...]:
    stmt = (
        repo._select()
        .where(Product.parent_product_id == parent_id, Product.deleted_at.is_(None))
        .with_only_columns(*_PRODUCT_COLUMNS)
    )
    rows = (await repo.db.execute(stmt)).all()
    # code-point order, independent of the database collation
    return tuple(sorted((_source(r) for r in rows), key=lambda p: (p.sku, str(p.id))))
