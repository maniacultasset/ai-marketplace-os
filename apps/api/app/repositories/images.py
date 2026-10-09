"""`ImageRepository`: the tenant's `images` rows (DATABASE.md:475-488; S14b T5 D6; S17b T4, T10a).

- `reports_for_product` (S14b): the stored report for one marketplace of a product's live images.
- S17b: the product image lock (advisory, transaction-scoped: the cap, the main replacement and
  the position of concurrent uploads of one product serialise), the tenant image-job lock and the
  live-job count (PLAN D15), `create`, the live/by-key reads, `soft_delete`, `write_report` (one
  UPDATE: report, worst-of `compliance_status`, the meta columns and `content_hash`), the product
  view's live keys, the orphan sweep's `registered_keys` and S19's `live_reports_for_images`.

"Live" is `deleted_at IS NULL`. A replaced image is soft-deleted and its object kept (spec L10193
"versions are kept"), so `get_by_key` and `registered_keys` count deleted rows too. Nothing here
runs a check or touches the object store.
"""

from __future__ import annotations

import builtins
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal
from uuid import UUID

from mkos_schemas.enums import ImageSlot, Marketplace
from mkos_schemas.images import ImageReport
from sqlalchemy import case, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.guard import TENANT_INFO_KEY, CrossTenantQueryError
from app.db.models.catalog import ProductColourway
from app.db.models.catalog_assets import Image
from app.db.models.ops import JobRun
from app.repositories.base import TenantScopedRepository

ComplianceStatus = Literal["pending", "passed", "failed"]
LIVE_JOB_STATES: Final = ("queued", "running")
# ProductOut.images order (CP7): the slot enum order, then position, then created_at, id.
SLOT_ORDER: Final[Mapping[str, int]] = {slot.value: i for i, slot in enumerate(ImageSlot)}
_PRODUCT_LOCK: Final = text(
    "SELECT pg_advisory_xact_lock(hashtextextended('images:' || CAST(:p AS text), 0))"
)
_TENANT_JOBS_LOCK: Final = text(
    "SELECT pg_advisory_xact_lock(hashtextextended('image_compliance:' || CAST(:t AS text), 0))"
)


@dataclass(frozen=True, slots=True)
class NewImage:
    id: UUID
    product_id: UUID
    colourway_id: UUID | None
    storage_key: str
    slot: ImageSlot
    bytes: int
    position: int


@dataclass(frozen=True, slots=True)
class ImageMeta:
    mime_type: str
    width_px: int
    height_px: int


def rollup(reports: Mapping[Any, ImageReport]) -> ComplianceStatus:
    """Worst of the per-marketplace reports: none → pending, any fail → failed, else passed."""
    if not reports:
        return "pending"
    return "failed" if any(not r.pass_ for r in reports.values()) else "passed"


def _uuid(name: str, value: object) -> UUID:
    if not isinstance(value, UUID):
        raise ValueError(f"{name} must be a UUID")
    return value


class _Colourways(TenantScopedRepository[ProductColourway]):
    model = ProductColourway

    async def style_of(self, colourway_id: UUID) -> UUID | None:
        stmt = (
            self._select()
            .where(ProductColourway.id == colourway_id, ProductColourway.deleted_at.is_(None))
            .with_only_columns(ProductColourway.style_product_id)
        )
        found: UUID | None = (await self.db.execute(stmt)).scalar_one_or_none()
        return found


class _ImageJobs(TenantScopedRepository[JobRun]):
    model = JobRun

    async def count_live(self) -> int:
        stmt = (
            self._select()
            .where(JobRun.kind == "image_compliance", JobRun.status.in_(LIVE_JOB_STATES))
            .with_only_columns(func.count(JobRun.id))
        )
        return int((await self.db.execute(stmt)).scalar_one())


class ImageRepository(TenantScopedRepository[Image]):
    model = Image

    def _own_session(self) -> None:
        if self.db.info.get(TENANT_INFO_KEY) != self.tenant_id:
            raise CrossTenantQueryError("images write for another tenant's unit of work")

    # ---- S14b ----

    async def reports_for_product(
        self, product_id: UUID, marketplace: str
    ) -> dict[UUID, ImageReport]:
        if not isinstance(product_id, UUID):
            raise ValueError("product_id must be a UUID")
        inner = (
            self._select()
            .where(
                Image.product_id == product_id,
                Image.deleted_at.is_(None),
                Image.image_report_json.has_key(marketplace),
            )
            .cte("own_images")
        )
        stmt = self._scoped_outer(
            select(inner.c.id, inner.c.image_report_json[marketplace]).order_by(inner.c.id), inner
        )
        rows = (await self.db.execute(stmt)).all()
        return {row[0]: ImageReport.model_validate(row[1]) for row in rows}

    # ---- locks and caps (T4, T10a) ----

    async def lock_product_images(self, product_id: UUID) -> None:
        """The product's image lock, held until the transaction ends."""
        self._own_session()
        await self.db.execute(_PRODUCT_LOCK, {"p": str(_uuid("product_id", product_id))})

    async def lock_tenant_image_jobs(self) -> None:
        """The tenant's image-job lock (PLAN D15(a)), taken before any product lock."""
        self._own_session()
        await self.db.execute(_TENANT_JOBS_LOCK, {"t": str(self.tenant_id)})

    async def count_live_image_jobs(self) -> int:
        """The tenant's `image_compliance` jobs that are `queued` or `running`."""
        return await _ImageJobs(self.db, self.tenant_id).count_live()

    async def colourway_style(self, colourway_id: UUID) -> UUID | None:
        """The style product of a live colourway of this tenant, else None."""
        return await _Colourways(self.db, self.tenant_id).style_of(
            _uuid("colourway_id", colourway_id)
        )

    # ---- writes ----

    async def create(self, new: NewImage) -> Image | None:
        """INSERT the row (`uploaded`, compliance `pending`). None when the tenant already has a
        row on that storage key (a concurrent register): the caller re-reads it."""
        self._own_session()
        stmt = (
            pg_insert(Image)
            .values(
                id=_uuid("id", new.id),
                tenant_id=self.tenant_id,
                product_id=_uuid("product_id", new.product_id),
                colourway_id=new.colourway_id,
                storage_key=new.storage_key,
                slot=ImageSlot(new.slot).value,
                status="uploaded",
                compliance_status="pending",
                bytes=new.bytes,
                position=new.position,
            )
            .on_conflict_do_nothing(index_elements=[Image.tenant_id, Image.storage_key])
            .returning(Image)
        )
        row: Image | None = (await self.db.execute(stmt)).scalar_one_or_none()
        return row

    async def soft_delete(self, image_id: UUID) -> bool:
        """Mark a live row deleted (its object stays); False when it is not live."""
        self._own_session()
        stmt = self._scoped_write(
            update(Image)
            .where(
                Image.id == _uuid("image_id", image_id),
                Image.deleted_at.is_(None),
                self._tenant_criterion(include_platform=False),
            )
            .values(deleted_at=func.now())
            .returning(Image.id)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none() is not None

    async def write_report(
        self,
        image_id: UUID,
        *,
        reports: Mapping[Marketplace, ImageReport],
        meta: ImageMeta | None,
        content_hash: str,
    ) -> ComplianceStatus | None:
        """One UPDATE of a live row: the report object keyed by marketplace (wire aliases, so
        `pass`), the worst-of `compliance_status`, the meta columns and `content_hash`. Returns
        the status written, or None when the row is not live any more."""
        self._own_session()
        status = rollup(reports)
        dumped = {
            Marketplace(m).value: r.model_dump(mode="json", by_alias=True)
            for m, r in reports.items()
        }
        values: dict[str, Any] = {
            "image_report_json": dumped,
            "compliance_status": status,
            "content_hash": content_hash,
            "mime_type": None if meta is None else meta.mime_type,
            "width_px": None if meta is None else meta.width_px,
            "height_px": None if meta is None else meta.height_px,
        }
        stmt = self._scoped_write(
            update(Image)
            .where(
                Image.id == _uuid("image_id", image_id),
                Image.deleted_at.is_(None),
                self._tenant_criterion(include_platform=False),
            )
            .values(**values)
            .returning(Image.id)
        )
        written = (await self.db.execute(stmt)).scalar_one_or_none()
        return None if written is None else status

    # ---- reads ----

    async def get_live(self, image_id: UUID) -> Image | None:
        stmt = self._select().where(
            Image.id == _uuid("image_id", image_id), Image.deleted_at.is_(None)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def get_by_key(self, storage_key: str) -> Image | None:
        """The row on this key, deleted rows included (PLAN D8)."""
        stmt = self._select().where(Image.storage_key == storage_key)
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def count_live(self, product_id: UUID) -> int:
        stmt = (
            self._select()
            .where(Image.product_id == _uuid("product_id", product_id), Image.deleted_at.is_(None))
            .with_only_columns(func.count(Image.id))
        )
        return int((await self.db.execute(stmt)).scalar_one())

    async def next_position(
        self, product_id: UUID, colourway_id: UUID | None, slot: ImageSlot
    ) -> int:
        """The next free position among live rows of (product, colourway, slot); 0 when none."""
        colourway = (
            Image.colourway_id.is_(None)
            if colourway_id is None
            else Image.colourway_id == colourway_id
        )
        stmt = (
            self._select()
            .where(
                Image.product_id == _uuid("product_id", product_id),
                colourway,
                Image.slot == ImageSlot(slot).value,
                Image.deleted_at.is_(None),
            )
            .with_only_columns(func.coalesce(func.max(Image.position) + 1, 0))
        )
        return int((await self.db.execute(stmt)).scalar_one())

    async def live_main(self, product_id: UUID, colourway_id: UUID | None) -> Image | None:
        colourway = (
            Image.colourway_id.is_(None)
            if colourway_id is None
            else Image.colourway_id == colourway_id
        )
        stmt = self._select().where(
            Image.product_id == _uuid("product_id", product_id),
            colourway,
            Image.slot == ImageSlot.MAIN.value,
            Image.deleted_at.is_(None),
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def live_keys_for_products(
        self, product_ids: Sequence[UUID]
    ) -> dict[UUID, builtins.list[str]]:
        """Each product's live storage keys in CP7 order, in one SELECT."""
        ids = [_uuid("product_id", p) for p in product_ids]
        if not ids:
            return {}
        slot_rank = case(SLOT_ORDER, value=Image.slot, else_=len(SLOT_ORDER))
        stmt = (
            self._select()
            .where(Image.product_id.in_(ids), Image.deleted_at.is_(None))
            .with_only_columns(Image.product_id, Image.storage_key)
            .order_by(Image.product_id, slot_rank, Image.position, Image.created_at, Image.id)
        )
        found: dict[UUID, builtins.list[str]] = {p: [] for p in ids}
        for product_id, key in (await self.db.execute(stmt)).all():
            found[product_id].append(key)
        return found

    async def registered_keys(self, keys: Sequence[str]) -> set[str]:
        """The keys among `keys` that have a row of this tenant (deleted rows count)."""
        if not keys:
            return set()
        stmt = (
            self._select()
            .where(Image.storage_key.in_(list(keys)))
            .with_only_columns(Image.storage_key)
        )
        return set((await self.db.execute(stmt)).scalars())

    async def live_reports_for_images(
        self, image_ids: Sequence[UUID]
    ) -> dict[UUID, dict[Marketplace, ImageReport]]:
        """S19's approve gate: the stored reports of the live images among `image_ids`, by
        marketplace, in one SELECT. A pending row maps to `{}`; unknown, foreign or deleted ids
        are absent."""
        ids = [_uuid("image_id", i) for i in image_ids]
        if not ids:
            return {}
        stmt = (
            self._select()
            .where(Image.id.in_(ids), Image.deleted_at.is_(None))
            .with_only_columns(Image.id, Image.image_report_json)
        )
        found: dict[UUID, dict[Marketplace, ImageReport]] = {}
        for image_id, stored in (await self.db.execute(stmt)).all():
            found[image_id] = {
                Marketplace(m): ImageReport.model_validate(r) for m, r in (stored or {}).items()
            }
        return found
