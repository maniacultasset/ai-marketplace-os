"""`brand_documents` (DATABASE.md:1055; S16 T9): the tenant's uploaded brand guides.

A live document has `deleted_at IS NULL`; `UNIQUE (tenant_id, content_hash) WHERE deleted_at IS
NULL` makes the same bytes one live document. Every job-side write is fenced on the document's
current `job_id` and on it being live, so an old job (or one whose document was deleted or
re-queued meanwhile) never overwrites newer state. `storage_key` and `content_hash` never leave
the service layer.
"""

from __future__ import annotations

import builtins
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, null, or_, update
from sqlalchemy.exc import IntegrityError

from app.db.guard import TENANT_INFO_KEY, CrossTenantQueryError
from app.db.integrity import to_conflict
from app.db.models.audit_brand import BrandDocument
from app.repositories.base import TenantScopedRepository

CLAIMABLE = ("uploaded", "processing")


@dataclass(frozen=True, slots=True)
class DocumentPage:
    items: builtins.list[BrandDocument]
    has_more: bool


class BrandDocumentRepository(TenantScopedRepository[BrandDocument]):
    model = BrandDocument

    def _own_session(self) -> None:
        if self.db.info.get(TENANT_INFO_KEY) != self.tenant_id:
            raise CrossTenantQueryError("brand_documents write for another tenant's unit of work")

    def _live(self, document_id: uuid.UUID) -> builtins.list[Any]:
        if not isinstance(document_id, uuid.UUID):
            raise ValueError("document_id must be a UUID")
        return [
            BrandDocument.id == document_id,
            BrandDocument.deleted_at.is_(None),
            self._tenant_criterion(include_platform=False),
        ]

    async def _update(self, where: builtins.list[Any], values: dict[str, Any]) -> int:
        self._own_session()
        stmt = self._scoped_write(update(BrandDocument).where(*where).values(**values))
        result = await self.db.execute(stmt)
        return int(getattr(result, "rowcount", 0) or 0)

    # ---- reads ----

    async def get_live(self, document_id: uuid.UUID) -> BrandDocument | None:
        stmt = self._select().where(
            BrandDocument.id == document_id, BrandDocument.deleted_at.is_(None)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def get_live_by_hash(self, content_hash: str) -> BrandDocument | None:
        stmt = self._select().where(
            BrandDocument.content_hash == content_hash, BrandDocument.deleted_at.is_(None)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def count_live_documents(self) -> int:
        stmt = (
            self._select().where(BrandDocument.deleted_at.is_(None)).with_only_columns(func.count())
        )
        return int((await self.db.execute(stmt)).scalar_one())

    async def list_page(
        self, *, limit: int, after: tuple[datetime, uuid.UUID] | None = None
    ) -> DocumentPage:
        stmt = self._select().where(BrandDocument.deleted_at.is_(None))
        if after is not None:
            at, last = after
            stmt = stmt.where(
                or_(
                    BrandDocument.created_at < at,
                    and_(BrandDocument.created_at == at, BrandDocument.id < last),
                )
            )
        stmt = stmt.order_by(BrandDocument.created_at.desc(), BrandDocument.id.desc()).limit(
            limit + 1
        )
        rows = builtins.list((await self.db.execute(stmt)).scalars())
        return DocumentPage(items=rows[:limit], has_more=len(rows) > limit)

    # ---- writes ----

    async def create(
        self,
        *,
        id: uuid.UUID,
        filename: str,
        content_type: str,
        bytes_: int,
        content_hash: str,
        storage_key: str,
        job_id: uuid.UUID,
        created_by: uuid.UUID,
    ) -> BrandDocument:
        document = BrandDocument(
            id=id,
            tenant_id=self.tenant_id,
            filename=filename,
            content_type=content_type,
            bytes=bytes_,
            content_hash=content_hash,
            storage_key=storage_key,
            job_id=job_id,
            created_by=created_by,
            status="uploaded",
        )
        return await self.add(document)

    async def claim_for_job(self, document_id: uuid.UUID, job_id: uuid.UUID) -> str | None:
        """`processing` for this job; the storage key, or None (gone, or another job's now)."""
        self._own_session()
        stmt = self._scoped_write(
            update(BrandDocument)
            .where(
                *self._live(document_id),
                BrandDocument.job_id == job_id,
                BrandDocument.status.in_(CLAIMABLE),
            )
            .values(status="processing")
        ).returning(BrandDocument.storage_key)
        key: str | None = (await self.db.execute(stmt)).scalar_one_or_none()
        return key

    async def lock_live_for_job(self, document_id: uuid.UUID, job_id: uuid.UUID) -> bool:
        stmt = (
            self._select()
            .where(
                BrandDocument.id == document_id,
                BrandDocument.deleted_at.is_(None),
                BrandDocument.job_id == job_id,
            )
            .with_only_columns(BrandDocument.id)
            .with_for_update()
        )
        return (await self.db.execute(stmt)).scalar_one_or_none() is not None

    async def mark_ready(self, document_id: uuid.UUID, job_id: uuid.UUID, chunk_count: int) -> bool:
        where = [*self._live(document_id), BrandDocument.job_id == job_id]
        values = {"status": "ready", "chunk_count": chunk_count, "error_report_json": null()}
        return await self._update(where, values) == 1

    async def mark_failed(
        self, document_id: uuid.UUID, job_id: uuid.UUID, report: dict[str, Any]
    ) -> bool:
        if not isinstance(report, dict) or not report:
            raise ValueError("a failed document needs its ErrorReport")
        where = [*self._live(document_id), BrandDocument.job_id == job_id]
        values = {"status": "failed", "chunk_count": 0, "error_report_json": report}
        return await self._update(where, values) == 1

    async def requeue_failed(self, document_id: uuid.UUID, new_job_id: uuid.UUID) -> bool:
        where = [*self._live(document_id), BrandDocument.status == "failed"]
        values = {"status": "uploaded", "job_id": new_job_id, "error_report_json": null()}
        return await self._update(where, values) == 1

    async def point_at_job(
        self, document_id: uuid.UUID, old_job: uuid.UUID, new_job: uuid.UUID
    ) -> bool:
        """The staff requeue (S13 RequeueHook): the document follows the new job."""
        where = [*self._live(document_id), BrandDocument.job_id == old_job]
        values = {"status": "uploaded", "job_id": new_job, "error_report_json": null()}
        return await self._update(where, values) == 1

    async def soft_delete(self, document_id: uuid.UUID) -> bool:
        try:
            return await self._update(self._live(document_id), {"deleted_at": func.now()}) == 1
        except IntegrityError as exc:  # pragma: no cover - no constraint names deleted_at
            raise to_conflict(exc) from exc
