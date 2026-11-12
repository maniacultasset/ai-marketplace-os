"""`ImageRepository`: the tenant's `images` rows (DATABASE.md:475-488; S14b T5 D6; S17b T4, T10a).

- `reports_for_product` (S14b): the stored report for one marketplace of a product's live images.
- S17b: the product image lock (advisory, transaction-scoped: the cap, the main replacement and
  the position of concurrent uploads of one product serialise), the tenant image-job lock and the
  live-job count (PLAN D15), `create`, the live/by-key reads, `soft_delete`, `write_report` (one
  UPDATE: report, worst-of `compliance_status`, the meta columns and `content_hash`), the product
  view's live keys, the orphan sweep's `registered_keys` and S19's `live_reports_for_images`.
- SI12: the photo `role` (Back, Close-up) and `clear_role`, the product's live size chart, and the
  size-chart read result stored on the image (`metadata.size_chart_read`, fenced on the clean hash).
- V1.1 (client feedback item 9): the photo match stored on the image (`metadata.photo_match`,
  fenced on the clean hash and the match key), the seller's keep, and the gate's read.

"Live" is `deleted_at IS NULL`. A replaced image is soft-deleted and its object kept (spec L10193
"versions are kept"), so `get_by_key` and `registered_keys` count deleted rows too. Nothing here
runs a check or touches the object store.
"""

from __future__ import annotations

import builtins
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal
from uuid import UUID

from mkos_schemas.enums import ImageSlot, Marketplace
from mkos_schemas.images import ImageReport
from sqlalchemy import and_, case, cast, func, literal, select, text, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from app.db.guard import TENANT_INFO_KEY, CrossTenantQueryError
from app.db.integrity import to_conflict
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
    # SI7: write-once provenance (`seller` unless made from a candidate)
    origin: str = "seller"
    parent_image_id: UUID | None = None
    candidate_id: UUID | None = None
    # SI12: `back` | `close_up` on a secondary image
    role: str | None = None


@dataclass(frozen=True, slots=True)
class ImageDetail:
    image_id: UUID
    slot: str
    colourway_id: UUID | None
    position: int
    status: str
    compliance_status: str
    # SI7: provenance, and what the product read needs to offer `fixes` (the stored reports and
    # border profile; neither is part of the wire model)
    origin: str = "seller"
    parent_image_id: UUID | None = None
    fixes: tuple[str, ...] = ()
    # B2: why a failed main photo offers no fix (`image_fixes.fix_unavailable_for`)
    fix_unavailable: str | None = None
    report: Mapping[str, Any] | None = None
    border: Mapping[str, Any] | None = None
    # SI12: the photo role, and what the size-chart suggestion needs (the read stored on the
    # image, the clean hash it must match, and the upload time to pick the newest chart)
    role: str | None = None
    size_chart_read: Mapping[str, Any] | None = None
    clean_content_hash: str | None = None
    created_at: datetime | None = None
    # image views: which derived copy exists to sign (`vision.jpg` when `metadata.vision` is
    # set, else the clean copy of `clean_mime_type`), and the signed GET the read adds
    has_vision: bool = False
    clean_mime_type: str | None = None
    view_url: str | None = None
    view_expires_at: datetime | None = None
    # V1.1: the stored `metadata.photo_match`, and the state the product read makes of it
    # (`app.services.photo_match.read_state`)
    photo_match_raw: Mapping[str, Any] | None = None
    photo_match: Any = None


@dataclass(frozen=True, slots=True)
class MainImageRow:
    """A live main image of a product, with what its signed view needs (the product list)."""

    product_id: UUID
    image_id: UUID
    colourway_id: UUID | None
    colour_key: str | None
    has_vision: bool
    clean_mime_type: str | None


@dataclass(frozen=True, slots=True)
class ListingImageRow:
    """A live image of a product as generation orders it (SI1 T18)."""

    image_id: UUID
    product_id: UUID
    slot: str
    colourway_id: UUID | None
    position: int
    clean_content_hash: str | None
    vision: Mapping[str, Any] | None
    colourway_position: int | None
    created_at: datetime
    # SI12: `back` | `close_up` on a secondary image
    role: str | None = None


@dataclass(frozen=True, slots=True)
class CleanMeta:
    """The stored clean derivative (SI1 T16): its sha256, its type and the vision size."""

    content_hash: str
    mime_type: str
    vision_w: int
    vision_h: int
    # SI7: the border profile `{uniform, near_white, colour}`; None from an older derive child
    border: Mapping[str, Any] | None = None


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
                origin=new.origin,
                parent_image_id=new.parent_image_id,
                candidate_id=new.candidate_id,
                role=new.role,
            )
            .on_conflict_do_nothing(index_elements=[Image.tenant_id, Image.storage_key])
            .returning(Image)
        )
        try:
            row: Image | None = (await self.db.execute(stmt)).scalar_one_or_none()
        except IntegrityError as exc:  # SI12: `images_one_role` lost a race (CP-D5)
            raise to_conflict(exc) from exc
        return row

    async def demote_main(self, image_id: UUID, position: int) -> bool:
        """SI7: the live main becomes a `secondary` image at `position` (an accepted candidate
        takes the main slot; `images_one_main` holds). False when it is not the live main."""
        self._own_session()
        stmt = self._scoped_write(
            update(Image)
            .where(
                Image.id == _uuid("image_id", image_id),
                Image.slot == ImageSlot.MAIN.value,
                Image.deleted_at.is_(None),
                self._tenant_criterion(include_platform=False),
            )
            .values(slot=ImageSlot.SECONDARY.value, position=position)
            .returning(Image.id)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none() is not None

    async def clear_role(
        self, product_id: UUID, colourway_id: UUID | None, role: str
    ) -> builtins.list[UUID]:
        """SI12: the live holders of `role` on this product and colourway lose it (they stay live
        `secondary` images), so a new upload can take it. Returns the ids cleared."""
        self._own_session()
        stmt = self._scoped_write(
            update(Image)
            .where(
                Image.product_id == _uuid("product_id", product_id),
                Image.colourway_id.is_not_distinct_from(colourway_id),
                Image.role == role,
                Image.deleted_at.is_(None),
                self._tenant_criterion(include_platform=False),
            )
            .values(role=None)
            .returning(Image.id)
        )
        return sorted((await self.db.execute(stmt)).scalars())

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

    async def soft_delete_for_products(self, product_ids: Sequence[UUID]) -> builtins.list[UUID]:
        """Product delete: mark every live image of these products deleted (objects stay; the
        daily `sweep_deleted_image_objects` removes the derived and public copies). The ids."""
        self._own_session()
        if not product_ids:
            return []
        stmt = self._scoped_write(
            update(Image)
            .where(
                Image.product_id.in_([_uuid("product_id", p) for p in product_ids]),
                Image.deleted_at.is_(None),
                self._tenant_criterion(include_platform=False),
            )
            .values(deleted_at=func.now())
            .returning(Image.id)
        )
        return sorted((await self.db.execute(stmt)).scalars())

    async def live_ids_for_products(self, product_ids: Sequence[UUID]) -> builtins.list[UUID]:
        """The live image ids of these products, in id order."""
        if not product_ids:
            return []
        stmt = (
            self._select()
            .where(Image.product_id.in_(list(product_ids)), Image.deleted_at.is_(None))
            .with_only_columns(Image.id)
            .order_by(Image.id)
        )
        return builtins.list((await self.db.execute(stmt)).scalars())

    async def write_report(
        self,
        image_id: UUID,
        *,
        reports: Mapping[Marketplace, ImageReport],
        meta: ImageMeta | None,
        content_hash: str,
        clean: CleanMeta | None = None,
    ) -> ComplianceStatus | None:
        """One UPDATE of a live row: the report object keyed by marketplace (wire aliases, so
        `pass`), the worst-of `compliance_status`, the meta columns and `content_hash`. Returns
        the status written, or None when the row is not live any more.

        SI1 T16: `clean` sets `clean_content_hash`, `clean_mime_type` and `metadata.vision`
        `{w, h}`; None clears them, except on a row that already has a `public_id` (its public
        copy was made from the stored clean copy, and `ck_images_public_needs_clean` keeps it)."""
        self._own_session()
        status = rollup(reports)
        dumped = {
            Marketplace(m).value: r.model_dump(mode="json", by_alias=True)
            for m, r in reports.items()
        }
        values: dict[Any, Any] = {
            "image_report_json": dumped,
            "compliance_status": status,
            "content_hash": content_hash,
            "mime_type": None if meta is None else meta.mime_type,
            "width_px": None if meta is None else meta.width_px,
            "height_px": None if meta is None else meta.height_px,
        }
        if clean is not None:
            vision = cast(literal(json.dumps({"w": clean.vision_w, "h": clean.vision_h})), JSONB)
            border = cast(literal(json.dumps(clean.border)), JSONB)
            values["clean_content_hash"] = clean.content_hash
            values["clean_mime_type"] = clean.mime_type
            merged = Image.metadata_.op("||")(func.jsonb_build_object("vision", vision))
            if clean.border is None:  # an older child: no stale profile is kept
                values[Image.metadata_] = merged.op("-")("border")
            else:
                values[Image.metadata_] = merged.op("||")(func.jsonb_build_object("border", border))
        else:
            published = Image.public_id.is_not(None)
            values["clean_content_hash"] = case((published, Image.clean_content_hash), else_=None)
            values["clean_mime_type"] = case((published, Image.clean_mime_type), else_=None)
            values[Image.metadata_] = case(
                (published, Image.metadata_),
                else_=Image.metadata_.op("-")("vision").op("-")("border"),
            )
        stmt = self._scoped_write(
            update(Image)
            .where(
                Image.id == _uuid("image_id", image_id),
                Image.deleted_at.is_(None),
                self._tenant_criterion(include_platform=False),
            )
            .values(values)
            .returning(Image.id)
        )
        written = (await self.db.execute(stmt)).scalar_one_or_none()
        return None if written is None else status

    # ---- SI1 T19/T21: the public copy (CP16) ----

    async def set_public_id(self, image_id: UUID, public_id: str) -> str | None:
        """Name a live row's public copy once: the stored name (this one, or the one a racing
        publish wrote), or None when the row is deleted. A name taken by another row raises
        the unique violation (`images_public_id_uq`) for the caller's retry."""
        self._own_session()
        iid = _uuid("image_id", image_id)
        stmt = self._scoped_write(
            update(Image)
            .where(
                Image.id == iid,
                Image.public_id.is_(None),
                Image.deleted_at.is_(None),
                self._tenant_criterion(include_platform=False),
            )
            .values(public_id=public_id)
            .returning(Image.public_id)
        )
        try:
            written: str | None = (await self.db.execute(stmt)).scalar_one_or_none()
        except IntegrityError as exc:
            raise to_conflict(exc) from exc
        if written is not None:
            return written
        found = (
            await self.db.execute(
                self._select()
                .where(Image.id == iid)
                .with_only_columns(Image.public_id, Image.deleted_at)
            )
        ).one_or_none()
        if found is None or found[1] is not None:
            return None
        stored: str | None = found[0]
        return stored

    async def mark_public_written(self, image_id: UUID, public_id: str) -> bool | None:
        """Record that the public object exists (whatever `deleted_at` is, CP16): True when the
        row turned out deleted, False when live, None when the name does not match."""
        self._own_session()
        stmt = self._scoped_write(
            update(Image)
            .where(
                Image.id == _uuid("image_id", image_id),
                Image.public_id == public_id,
                self._tenant_criterion(include_platform=False),
            )
            .values(public_written_at=func.now())
            .returning(Image.deleted_at.is_not(None))
        )
        deleted: bool | None = (await self.db.execute(stmt)).scalar_one_or_none()
        return deleted

    async def clear_objects(self, image_id: UUID) -> bool:
        """The cleanup marker of a soft-deleted row whose objects are gone (decision 21): NULL
        `public_written_at`, the clean pair and `metadata.vision`. `public_id` stays as a
        tombstone (CP16), so the tenant purge step can still find the name (CP9)."""
        self._own_session()
        stmt = self._scoped_write(
            update(Image)
            .where(
                Image.id == _uuid("image_id", image_id),
                Image.deleted_at.is_not(None),
                self._tenant_criterion(include_platform=False),
            )
            .values(
                {
                    Image.public_written_at: None,
                    Image.clean_content_hash: None,
                    Image.clean_mime_type: None,
                    Image.metadata_: Image.metadata_.op("-")("vision"),
                }
            )
            .returning(Image.id)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none() is not None

    async def deleted_with_objects(self, limit: int) -> builtins.list[Image]:
        """Soft-deleted rows whose derived or public objects may still exist (the daily step)."""
        stmt = (
            self._select()
            .where(
                Image.deleted_at.is_not(None),
                (Image.public_written_at.is_not(None)) | (Image.clean_content_hash.is_not(None)),
            )
            .order_by(Image.deleted_at, Image.id)
            .limit(limit)
        )
        return builtins.list((await self.db.execute(stmt)).scalars())

    async def live_without_derivatives(self) -> builtins.list[UUID]:
        """Live images checked (passed or failed) but with no clean copy (`images derive`)."""
        stmt = (
            self._select()
            .where(
                Image.deleted_at.is_(None),
                Image.clean_content_hash.is_(None),
                Image.compliance_status.in_(("passed", "failed")),
            )
            .with_only_columns(Image.id)
            .order_by(Image.created_at, Image.id)
        )
        return builtins.list((await self.db.execute(stmt)).scalars())

    async def live_judged_by_old_rules(self) -> builtins.list[UUID]:
        """Live checked images whose stored report has an issue with no `severity`: judged
        before the 2.1.0 photo rules (`images recheck`), with no image job queued or running."""
        live_job = (
            select(JobRun.id)
            .where(
                JobRun.tenant_id == self.tenant_id,
                JobRun.kind == "image_compliance",
                JobRun.status.in_(LIVE_JOB_STATES),
                JobRun.entity_id == Image.id,
            )
            .exists()
        )
        stmt = (
            self._select()
            .where(
                Image.deleted_at.is_(None),
                Image.compliance_status.in_(("passed", "failed")),
                func.jsonb_path_exists(
                    Image.image_report_json,
                    text(
                        "'$.*.issues[*] ? (!(exists(@.severity)) || @.severity == null)'::jsonpath"
                    ),
                ),
                ~live_job,
            )
            .with_only_columns(Image.id)
            .order_by(Image.created_at, Image.id)
        )
        return builtins.list((await self.db.execute(stmt)).scalars())

    async def get_any(self, image_id: UUID) -> Image | None:
        """The row, deleted or not (the after-commit object removal reads it)."""
        stmt = self._select().where(Image.id == _uuid("image_id", image_id))
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def clear_public_written(self, image_id: UUID) -> None:
        """The public object is gone again (the post-PUT delete of a deleted row, CP16)."""
        self._own_session()
        await self.db.execute(
            self._scoped_write(
                update(Image)
                .where(
                    Image.id == _uuid("image_id", image_id),
                    self._tenant_criterion(include_platform=False),
                )
                .values(public_written_at=None)
            )
        )

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

    async def details_for_product(self, product_id: UUID) -> builtins.list[ImageDetail]:
        """The product's live images (a style's own and its colourways'), in CP7 order."""
        pid = _uuid("product_id", product_id)
        slot_rank = case(SLOT_ORDER, value=Image.slot, else_=len(SLOT_ORDER))
        stmt = (
            self._select()
            .where(Image.product_id == pid, Image.deleted_at.is_(None))
            .with_only_columns(
                Image.id,
                Image.slot,
                Image.colourway_id,
                Image.position,
                Image.status,
                Image.compliance_status,
                Image.origin,
                Image.parent_image_id,
                Image.image_report_json,
                Image.metadata_["border"].label("border"),
                Image.role,
                Image.metadata_["size_chart_read"].label("size_chart_read"),
                Image.clean_content_hash,
                Image.created_at,
                Image.metadata_["vision"].label("vision"),
                Image.clean_mime_type,
                Image.metadata_["photo_match"].label("photo_match"),
            )
            .order_by(slot_rank, Image.position, Image.created_at, Image.id)
        )
        return [
            ImageDetail(
                image_id=row.id,
                slot=row.slot,
                colourway_id=row.colourway_id,
                position=row.position,
                status=row.status,
                compliance_status=row.compliance_status,
                origin=row.origin,
                parent_image_id=row.parent_image_id,
                report=row.image_report_json,
                border=row.border if isinstance(row.border, Mapping) else None,
                role=row.role,
                size_chart_read=(
                    row.size_chart_read if isinstance(row.size_chart_read, Mapping) else None
                ),
                clean_content_hash=row.clean_content_hash,
                created_at=row.created_at,
                has_vision=isinstance(row.vision, Mapping),
                clean_mime_type=row.clean_mime_type,
                photo_match_raw=row.photo_match if isinstance(row.photo_match, Mapping) else None,
            )
            for row in (await self.db.execute(stmt)).all()
        ]

    async def main_images_for_products(
        self, product_ids: Sequence[UUID]
    ) -> builtins.list[MainImageRow]:
        """The live main images of these products (a style's own and its colourways'), each with
        its colourway's colour key: the style-level one first, then colourway position. Two
        scoped reads, as `listing_set`."""
        ids = [_uuid("product_id", p) for p in product_ids]
        if not ids:
            return []
        stmt = (
            self._select()
            .where(
                Image.product_id.in_(ids),
                Image.slot == ImageSlot.MAIN.value,
                Image.deleted_at.is_(None),
            )
            .with_only_columns(
                Image.product_id,
                Image.id,
                Image.colourway_id,
                Image.metadata_["vision"].label("vision"),
                Image.clean_mime_type,
                Image.created_at,
            )
        )
        rows = (await self.db.execute(stmt)).all()
        colourway_ids = sorted({r.colourway_id for r in rows if r.colourway_id is not None})
        colourways: dict[UUID, tuple[int, str]] = {}
        if colourway_ids:
            found = await self.db.execute(
                _Colourways(self.db, self.tenant_id)
                ._select()
                .where(ProductColourway.id.in_(colourway_ids))
                .with_only_columns(
                    ProductColourway.id, ProductColourway.position, ProductColourway.colour_key
                )
            )
            colourways = {cid: (int(pos), str(key)) for cid, pos, key in found.all()}

        def rank(row: Any) -> tuple[str, int, int, datetime, str]:
            cw = None if row.colourway_id is None else colourways.get(row.colourway_id)
            return (
                str(row.product_id),
                0 if row.colourway_id is None else 1,
                -1 if cw is None else cw[0],
                row.created_at,
                str(row.id),
            )

        out: builtins.list[MainImageRow] = []
        for row in sorted(rows, key=rank):
            cw = None if row.colourway_id is None else colourways.get(row.colourway_id)
            out.append(
                MainImageRow(
                    product_id=row.product_id,
                    image_id=row.id,
                    colourway_id=row.colourway_id,
                    colour_key=None if cw is None else cw[1],
                    has_vision=isinstance(row.vision, Mapping),
                    clean_mime_type=row.clean_mime_type,
                )
            )
        return out

    async def listing_set(self, product_id: UUID) -> builtins.list[ListingImageRow]:
        """The live images of a product (a style's own and its colourways' share `product_id`),
        with each colourway's position for the listing order (SI1 T18). Two scoped reads."""
        pid = _uuid("product_id", product_id)
        stmt = (
            self._select()
            .where(Image.product_id == pid, Image.deleted_at.is_(None))
            .with_only_columns(
                Image.id,
                Image.product_id,
                Image.slot,
                Image.colourway_id,
                Image.position,
                Image.clean_content_hash,
                Image.metadata_,
                Image.created_at,
                Image.role,
            )
        )
        rows = (await self.db.execute(stmt)).all()
        colourway_ids = sorted({r[3] for r in rows if r[3] is not None})
        positions: dict[UUID, int] = {}
        if colourway_ids:
            found = await self.db.execute(
                _Colourways(self.db, self.tenant_id)
                ._select()
                .where(ProductColourway.id.in_(colourway_ids))
                .with_only_columns(ProductColourway.id, ProductColourway.position)
            )
            positions = {cid: int(pos) for cid, pos in found.all()}
        out: builtins.list[ListingImageRow] = []
        for image_id, owner, slot, colourway, position, clean, metadata, created, role in rows:
            vision = metadata.get("vision") if isinstance(metadata, dict) else None
            out.append(
                ListingImageRow(
                    image_id=image_id,
                    product_id=owner,
                    slot=slot,
                    colourway_id=colourway,
                    position=position,
                    clean_content_hash=clean,
                    vision=vision if isinstance(vision, dict) else None,
                    colourway_position=None if colourway is None else positions.get(colourway),
                    created_at=created,
                    role=role,
                )
            )
        return out

    # ---- SI12: the size chart and its read ----

    async def live_size_chart(self, product_id: UUID) -> Image | None:
        """The product's size chart (CP-D3): its newest live style-level `size_chart` image."""
        stmt = (
            self._select()
            .where(
                Image.product_id == _uuid("product_id", product_id),
                Image.slot == ImageSlot.SIZE_CHART.value,
                Image.colourway_id.is_(None),
                Image.deleted_at.is_(None),
            )
            .order_by(Image.created_at.desc(), Image.id.desc())
            .limit(1)
        )
        found: Image | None = (await self.db.execute(stmt)).scalars().first()
        return found

    async def size_chart_of(self, product_id: UUID, image_id: UUID) -> Image | None:
        """A live `size_chart` image of this product, else None (a foreign, deleted or other
        product's image is None alike)."""
        stmt = self._select().where(
            Image.id == _uuid("image_id", image_id),
            Image.product_id == _uuid("product_id", product_id),
            Image.slot == ImageSlot.SIZE_CHART.value,
            Image.deleted_at.is_(None),
        )
        found: Image | None = (await self.db.execute(stmt)).scalars().first()
        return found

    async def write_size_chart_read(
        self, image_id: UUID, value: Mapping[str, Any], *, source_hash: str, job_id: UUID
    ) -> bool:
        """Store one read result as `metadata.size_chart_read` (with `source_hash` and `job_id`)
        on a live `size_chart` image whose clean copy still has `source_hash`. Another job's
        `read` or `unreadable` result for the same hash is never overwritten. False when nothing
        was written."""
        self._own_session()
        stored = {**value, "source_hash": source_hash, "job_id": str(job_id)}
        current = Image.metadata_["size_chart_read"]
        settled = (
            current["source_hash"].astext == source_hash,
            current["job_id"].astext != str(job_id),
            current["status"].astext.in_(("read", "unreadable")),
        )
        stmt = self._scoped_write(
            update(Image)
            .where(
                Image.id == _uuid("image_id", image_id),
                Image.slot == ImageSlot.SIZE_CHART.value,
                Image.deleted_at.is_(None),
                Image.clean_content_hash == source_hash,
                ~func.coalesce(and_(*settled), False),
                self._tenant_criterion(include_platform=False),
            )
            .values(
                {
                    Image.metadata_: Image.metadata_.op("||")(
                        func.jsonb_build_object(
                            "size_chart_read", cast(literal(json.dumps(stored)), JSONB)
                        )
                    )
                }
            )
            .returning(Image.id)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none() is not None

    # ---- V1.1: the variant photo match ----

    async def write_photo_match(
        self,
        image_id: UUID,
        value: Mapping[str, Any],
        *,
        source_hash: str,
        expect_key: str | None = None,
        expect_job: str | None = None,
    ) -> bool:
        """Store `value` as `metadata.photo_match` on a live image whose clean copy still has
        `source_hash`; with `expect_key` / `expect_job`, only while the stored match still names
        that key / job (a newer check, or other pixels, is never overwritten). False when nothing
        was written."""
        self._own_session()
        current = Image.metadata_["photo_match"]
        fences = [
            Image.id == _uuid("image_id", image_id),
            Image.deleted_at.is_(None),
            Image.clean_content_hash == source_hash,
            self._tenant_criterion(include_platform=False),
        ]
        if expect_key is not None:
            fences.append(current["key"].astext == expect_key)
        if expect_job is not None:
            fences.append(current["job_id"].astext == expect_job)
        stmt = self._scoped_write(
            update(Image)
            .where(*fences)
            .values(
                {
                    Image.metadata_: Image.metadata_.op("||")(
                        func.jsonb_build_object(
                            "photo_match", cast(literal(json.dumps(dict(value))), JSONB)
                        )
                    )
                }
            )
            .returning(Image.id)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none() is not None

    async def keep_photo_match(self, image_id: UUID, *, user_id: UUID, at: datetime) -> bool:
        """The seller keeps a photo the match questioned: `kept_by` and `kept_at` join the stored
        `mismatch` or `unsure` match of a live image, once (a second keep changes nothing). False
        when nothing was written."""
        self._own_session()
        current = Image.metadata_["photo_match"]
        kept = cast(
            literal(json.dumps({"kept_by": str(user_id), "kept_at": at.isoformat()})), JSONB
        )
        stmt = self._scoped_write(
            update(Image)
            .where(
                Image.id == _uuid("image_id", image_id),
                Image.deleted_at.is_(None),
                current["status"].astext.in_(("mismatch", "unsure")),
                current["kept_at"].is_(None),
                self._tenant_criterion(include_platform=False),
            )
            .values(
                {
                    Image.metadata_: Image.metadata_.op("||")(
                        func.jsonb_build_object("photo_match", current.op("||")(kept))
                    )
                }
            )
            .returning(Image.id)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none() is not None

    async def photo_matches_for_images(
        self, image_ids: Sequence[UUID]
    ) -> dict[UUID, tuple[Mapping[str, Any] | None, str | None]]:
        """The approve gate: `(metadata.photo_match, clean_content_hash)` of the live images among
        `image_ids`, in one SELECT; unknown, foreign or deleted ids are absent."""
        ids = [_uuid("image_id", i) for i in image_ids]
        if not ids:
            return {}
        stmt = (
            self._select()
            .where(Image.id.in_(ids), Image.deleted_at.is_(None))
            .with_only_columns(Image.id, Image.metadata_["photo_match"], Image.clean_content_hash)
        )
        return {
            image_id: (raw if isinstance(raw, Mapping) else None, clean)
            for image_id, raw, clean in (await self.db.execute(stmt)).all()
        }

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
