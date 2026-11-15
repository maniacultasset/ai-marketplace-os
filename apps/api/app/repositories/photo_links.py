"""`PhotoLinkRepository` (G2): the tenant's photo links from spreadsheet imports.

One row per link per target (product, colourway), deduped on the direct link's sha256
(`uq_photo_links_dedupe`). The import writes rows `queued` (or `skipped`); the fetch job claims
them (`fetching`, its `job_id`), then marks each `saved` with the image or `failed` with a closed
problem, every write fenced on the claiming job, so a stale job never overwrites a newer one.
Statuses: `queued`, `fetching`, `saved`, `failed`, `skipped`, `dismissed` (hidden by the seller).

Writes go through the tenant's own unit of work (RLS and the repository criterion both apply).
Nothing here logs or returns a link outside the row itself.
"""

from __future__ import annotations

import builtins
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

from sqlalchemy import func, or_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.guard import TENANT_INFO_KEY, CrossTenantQueryError
from app.db.models.catalog_assets import PhotoLink
from app.repositories.base import TenantScopedRepository

__all__ = ["RETRYABLE_SKIPS", "WAITING", "LinkTotals", "PhotoLinkRepository", "PlannedLink"]

WAITING: Final = ("queued", "fetching")
# a skip a retry may lift (the per-import cap); the others are the seller's or the product's
RETRYABLE_SKIPS: Final = ("import_cap",)
SWEEP_LIMIT: Final = 500


@dataclass(frozen=True, slots=True)
class PlannedLink:
    product_id: UUID
    colourway_id: UUID | None
    url: str
    url_hash: str
    source_column: str
    position: int
    import_id: UUID | None
    row_number: int | None
    requested_by: UUID | None


@dataclass(frozen=True, slots=True)
class LinkTotals:
    total: int
    waiting: int
    saved: int
    failed: int


def _uuid(name: str, value: object) -> UUID:
    if not isinstance(value, UUID):
        raise ValueError(f"{name} must be a UUID")
    return value


class PhotoLinkRepository(TenantScopedRepository[PhotoLink]):
    model = PhotoLink

    def _own_session(self) -> None:
        if self.db.info.get(TENANT_INFO_KEY) != self.tenant_id:
            raise CrossTenantQueryError("photo link write for another tenant's unit of work")

    def _update(self, *where: Any) -> Any:
        return update(PhotoLink).where(self._tenant_criterion(include_platform=False), *where)

    async def _ids(self, stmt: Any) -> builtins.list[UUID]:
        self._own_session()
        scoped = self._scoped_write(stmt.returning(PhotoLink.id))
        return list((await self.db.execute(scoped)).scalars())

    # ---- the import's plan ----

    async def find(
        self, product_id: UUID, colourway_id: UUID | None, url_hash: str
    ) -> PhotoLink | None:
        stmt = self._select().where(
            PhotoLink.product_id == _uuid("product_id", product_id),
            PhotoLink.colourway_id.is_not_distinct_from(colourway_id),
            PhotoLink.url_hash == url_hash,
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def insert(
        self, planned: PlannedLink, *, status: str = "queued", problem: str | None = None
    ) -> PhotoLink | None:
        """INSERT the row; None when the dedupe index already holds one (the caller re-reads)."""
        self._own_session()
        stmt = (
            pg_insert(PhotoLink)
            .values(
                tenant_id=self.tenant_id,
                product_id=_uuid("product_id", planned.product_id),
                colourway_id=planned.colourway_id,
                url=planned.url,
                url_hash=planned.url_hash,
                source_column=planned.source_column,
                position=planned.position,
                status=status,
                problem=problem,
                import_id=planned.import_id,
                row_number=planned.row_number,
                requested_by=planned.requested_by,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    PhotoLink.tenant_id,
                    PhotoLink.product_id,
                    PhotoLink.colourway_id,
                    PhotoLink.url_hash,
                ]
            )
            .returning(PhotoLink)
        )
        row: PhotoLink | None = (await self.db.execute(stmt)).scalar_one_or_none()
        return row

    async def replan(
        self,
        link_id: UUID,
        planned: PlannedLink,
        *,
        status: str | None,
        problem: str | None = None,
        provenance: bool = True,
    ) -> bool:
        """A re-import names a stored link again: its new status (None keeps it) and, unless
        `provenance` is False, this import's row and position. A requeue clears the outcome."""
        values: dict[str, Any] = {}
        if provenance:
            values.update(
                import_id=planned.import_id,
                row_number=planned.row_number,
                position=planned.position,
                source_column=planned.source_column,
                requested_by=planned.requested_by,
            )
        if status is not None:
            values.update(status=status, problem=problem)
            if status == "queued":
                values.update(http_status=None, file_type=None, job_id=None)
        if not values:
            return False
        stmt = self._update(PhotoLink.id == _uuid("link_id", link_id)).values(**values)
        return bool(await self._ids(stmt))

    # ---- the fetch job ----

    async def claim(self, product_id: UUID, job_id: UUID) -> builtins.list[PhotoLink]:
        """The product's `queued` links, and this job's own `fetching` ones (a re-run after a
        crash), now `fetching` under `job_id`; in colourway, then position order."""
        stmt = self._update(
            PhotoLink.product_id == _uuid("product_id", product_id),
            or_(
                PhotoLink.status == "queued",
                (PhotoLink.status == "fetching") & (PhotoLink.job_id == _uuid("job_id", job_id)),
            ),
        ).values(status="fetching", job_id=job_id, attempts=PhotoLink.attempts + 1)
        ids = await self._ids(stmt)
        if not ids:
            return []
        rows = self._select().where(PhotoLink.id.in_(ids))
        found = list((await self.db.execute(rows)).scalars())
        return sorted(
            found,
            key=lambda r: (r.colourway_id is not None, str(r.colourway_id or ""), r.position,
                           r.created_at, str(r.id)),
        )  # fmt: skip

    async def mark_saved(self, link_id: UUID, job_id: UUID, image_id: UUID) -> bool:
        stmt = self._update(
            PhotoLink.id == _uuid("link_id", link_id),
            PhotoLink.job_id == _uuid("job_id", job_id),
            PhotoLink.status == "fetching",
        ).values(
            status="saved", problem=None, file_type=None, image_id=image_id, fetched_at=func.now()
        )
        return bool(await self._ids(stmt))

    async def mark_failed(
        self,
        link_id: UUID,
        job_id: UUID,
        problem: str,
        http_status: int | None = None,
        file_type: str | None = None,
    ) -> bool:
        stmt = self._update(
            PhotoLink.id == _uuid("link_id", link_id),
            PhotoLink.job_id == _uuid("job_id", job_id),
            PhotoLink.status == "fetching",
        ).values(
            status="failed",
            problem=problem,
            http_status=http_status,
            file_type=file_type,
            fetched_at=func.now(),
        )
        return bool(await self._ids(stmt))

    async def release(
        self, job_id: UUID, link_ids: Sequence[UUID] | None = None
    ) -> builtins.list[UUID]:
        """This job's `fetching` links (all, or `link_ids`) back to `queued`."""
        where: list[Any] = [
            PhotoLink.job_id == _uuid("job_id", job_id),
            PhotoLink.status == "fetching",
        ]
        if link_ids is not None:
            if not link_ids:
                return []
            where.append(PhotoLink.id.in_([_uuid("link_id", i) for i in link_ids]))
        return await self._ids(self._update(*where).values(status="queued"))

    async def cancel_for_products(self, product_ids: Sequence[UUID]) -> builtins.list[UUID]:
        """A deleted product's waiting links: `skipped/product_gone`."""
        ids = [_uuid("product_id", p) for p in product_ids]
        if not ids:
            return []
        stmt = self._update(PhotoLink.product_id.in_(ids), PhotoLink.status.in_(WAITING)).values(
            status="skipped", problem="product_gone"
        )
        return await self._ids(stmt)

    # ---- seller actions ----

    async def get_link(self, link_id: UUID) -> PhotoLink | None:
        stmt = self._select().where(PhotoLink.id == _uuid("link_id", link_id))
        return (await self.db.execute(stmt)).scalar_one_or_none()

    def _retryable(self) -> Any:
        return or_(
            PhotoLink.status == "failed",
            (PhotoLink.status == "skipped") & PhotoLink.problem.in_(RETRYABLE_SKIPS),
        )

    async def requeue_failed(
        self,
        *,
        product_id: UUID | None = None,
        import_id: UUID | None = None,
        requested_by: UUID | None = None,
    ) -> builtins.list[tuple[UUID, UUID]]:
        """The failed (and import-capped) links of one product or one import back to `queued`,
        now asked for by `requested_by` (the member who pressed Retry: the stored photo's audit
        actor); the (link id, product id) pairs requeued."""
        if (product_id is None) == (import_id is None):
            raise ValueError("requeue one product or one import")
        target = (
            PhotoLink.product_id == _uuid("product_id", product_id)
            if product_id is not None
            else PhotoLink.import_id == _uuid("import_id", import_id)
        )
        self._own_session()
        stmt = self._scoped_write(
            self._update(target, self._retryable())
            .values(
                status="queued",
                problem=None,
                http_status=None,
                file_type=None,
                job_id=None,
                **({"requested_by": requested_by} if requested_by is not None else {}),
            )
            .returning(PhotoLink.id, PhotoLink.product_id)
        )
        return [(row[0], row[1]) for row in (await self.db.execute(stmt)).all()]

    async def dismiss(self, link_id: UUID) -> bool:
        stmt = self._update(
            PhotoLink.id == _uuid("link_id", link_id),
            PhotoLink.status.in_(("failed", "skipped")),
        ).values(status="dismissed")
        return bool(await self._ids(stmt))

    # ---- reads ----

    async def for_product(self, product_id: UUID) -> builtins.list[PhotoLink]:
        """A product's links (a style's colourway links included), dismissed ones left out."""
        stmt = (
            self._select()
            .where(
                PhotoLink.product_id == _uuid("product_id", product_id),
                PhotoLink.status != "dismissed",
            )
            .order_by(
                PhotoLink.colourway_id.is_not(None),
                PhotoLink.colourway_id,
                PhotoLink.position,
                PhotoLink.created_at,
                PhotoLink.id,
            )
        )
        return list((await self.db.execute(stmt)).scalars())

    async def waiting_for_product(self, product_id: UUID) -> bool:
        stmt = (
            self._select()
            .where(
                PhotoLink.product_id == _uuid("product_id", product_id),
                PhotoLink.status.in_(WAITING),
            )
            .with_only_columns(PhotoLink.id)
            .limit(1)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none() is not None

    async def for_import_rows(
        self, import_id: UUID, row_numbers: Sequence[int]
    ) -> dict[int, builtins.list[PhotoLink]]:
        """The links each of these import rows owns, in position order (one SELECT)."""
        numbers = sorted({int(n) for n in row_numbers})
        found: dict[int, builtins.list[PhotoLink]] = {n: [] for n in numbers}
        if not numbers:
            return found
        stmt = (
            self._select()
            .where(
                PhotoLink.import_id == _uuid("import_id", import_id),
                PhotoLink.row_number.in_(numbers),
                PhotoLink.status != "dismissed",
            )
            .order_by(PhotoLink.row_number, PhotoLink.position, PhotoLink.id)
        )
        for row in (await self.db.execute(stmt)).scalars():
            if row.row_number is not None:
                found.setdefault(row.row_number, []).append(row)
        return found

    async def totals_for_import(self, import_id: UUID) -> LinkTotals | None:
        """The import's link counts by outcome, or None when it named no link."""
        stmt = (
            self._select()
            .where(
                PhotoLink.import_id == _uuid("import_id", import_id),
                PhotoLink.status != "dismissed",
            )
            .with_only_columns(PhotoLink.status, func.count(PhotoLink.id))
            .group_by(PhotoLink.status)
        )
        counts = {str(s): int(n) for s, n in (await self.db.execute(stmt)).all()}
        total = sum(counts.values())
        if total == 0:
            return None
        return LinkTotals(
            total=total,
            waiting=counts.get("queued", 0) + counts.get("fetching", 0),
            saved=counts.get("saved", 0),
            failed=counts.get("failed", 0) + counts.get("skipped", 0),
        )

    async def products_with_queued(self, limit: int = SWEEP_LIMIT) -> builtins.list[UUID]:
        """Products with a `queued` link (the sweeper's pass), oldest first."""
        stmt = (
            self._select()
            .where(PhotoLink.status == "queued")
            .with_only_columns(PhotoLink.product_id, func.min(PhotoLink.created_at).label("at"))
            .group_by(PhotoLink.product_id)
            .order_by(func.min(PhotoLink.created_at), PhotoLink.product_id)
            .limit(limit)
        )
        return [row[0] for row in (await self.db.execute(stmt)).all()]
