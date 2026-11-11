"""`ImageCandidateRepository` (SI7 T9, ADR-0030 proposed): the tenant's proposed images.

The cache is the partial unique index `uq_image_candidates_cache` over (tenant, product, kind,
source hash, generator key), outside the retryable failures and the accepted rows (0030). `create_or_get` inserts against it
and reads the winner, so two concurrent creates make one row and exactly one `created=True`;
only that caller enqueues. Every outcome write is fenced on the job that owns the row
(`job_id`) and on `status = 'queued'`, so a stale job can never overwrite a newer one, and
`set_job` sets a job only on a row that has none.

Writes go through the tenant's own unit of work (RLS and the repository criterion both apply).
"""

from __future__ import annotations

import builtins
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

from mkos_schemas.codes import ErrorCode
from sqlalchemy import delete, exists, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.errors import ConflictError
from app.db.guard import TENANT_INFO_KEY, CrossTenantQueryError
from app.db.models.catalog_assets import (
    CANDIDATE_CACHE_WHERE,
    RETRYABLE_FAILURES,
    ImageCandidate,
)
from app.db.models.ops import JobRun
from app.repositories.base import TenantScopedRepository
from app.repositories.budget import day_bounds

__all__ = ["RETRYABLE_FAILURES", "ImageCandidateRepository", "NewCandidate"]

AI_KIND: Final = "white_background"
LIST_MAX: Final = 50
_DECIDABLE: Final = ("queued", "ready", "rejected")


@dataclass(frozen=True, slots=True)
class NewCandidate:
    id: UUID
    product_id: UUID
    colourway_id: UUID | None
    source_image_id: UUID | None
    kind: str
    source_hash: str
    generator_key: str
    created_by: UUID | None
    status: str = "queued"
    failure_reason: str | None = None


def _uuid(name: str, value: object) -> UUID:
    if not isinstance(value, UUID):
        raise ValueError(f"{name} must be a UUID")
    return value


class ImageCandidateRepository(TenantScopedRepository[ImageCandidate]):
    model = ImageCandidate

    def _own_session(self) -> None:
        if self.db.info.get(TENANT_INFO_KEY) != self.tenant_id:
            raise CrossTenantQueryError("image candidate write for another tenant's unit of work")

    def _fenced(self, candidate_id: UUID, *where: Any) -> Any:
        return update(ImageCandidate).where(
            ImageCandidate.id == _uuid("candidate_id", candidate_id),
            self._tenant_criterion(include_platform=False),
            *where,
        )

    async def _write(self, stmt: Any) -> bool:
        self._own_session()
        scoped = self._scoped_write(stmt.returning(ImageCandidate.id))
        return (await self.db.execute(scoped)).scalar_one_or_none() is not None

    # ---- create ----

    async def create_or_get(
        self, new: NewCandidate, *, _retries: int = 1
    ) -> tuple[ImageCandidate, bool]:
        """INSERT … ON CONFLICT on the cache index DO NOTHING, then the winner. `created` is True
        only for this call's own insert. A winner that is gone again is retried once, then 409
        (never a loop)."""
        self._own_session()
        stmt = (
            pg_insert(ImageCandidate)
            .values(
                id=_uuid("id", new.id),
                tenant_id=self.tenant_id,
                product_id=_uuid("product_id", new.product_id),
                colourway_id=new.colourway_id,
                source_image_id=new.source_image_id,
                kind=new.kind,
                status=new.status,
                failure_reason=new.failure_reason,
                source_hash=new.source_hash,
                generator_key=new.generator_key,
                created_by=new.created_by,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    ImageCandidate.tenant_id,
                    ImageCandidate.product_id,
                    ImageCandidate.kind,
                    ImageCandidate.source_hash,
                    ImageCandidate.generator_key,
                ],
                index_where=text(CANDIDATE_CACHE_WHERE),
            )
            .returning(ImageCandidate.id)
        )
        inserted = (await self.db.execute(stmt)).scalar_one_or_none()
        if inserted is not None:
            row = await self.get(inserted)
            assert row is not None  # noqa: S101 - inserted in this transaction
            return row, True
        winner = await self.find_cached(
            new.product_id, new.kind, new.source_hash, new.generator_key
        )
        if winner is None:  # the winner was rolled back between the insert and the read: retry
            if _retries <= 0:
                raise ConflictError(ErrorCode.INVALID_STATUS_TRANSITION, "image_candidates")
            return await self.create_or_get(new, _retries=_retries - 1)
        return winner, False

    # ---- reads ----

    async def find_cached(
        self, product_id: UUID, kind: str, source_hash: str, generator_key: str
    ) -> ImageCandidate | None:
        """The row the cache index holds for this key (any covered status, `queued` included)."""
        stmt = (
            self._select()
            .where(
                ImageCandidate.product_id == _uuid("product_id", product_id),
                ImageCandidate.kind == kind,
                ImageCandidate.source_hash == source_hash,
                ImageCandidate.generator_key == generator_key,
                text(CANDIDATE_CACHE_WHERE),
            )
            .execution_options(populate_existing=True)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def latest_accepted(
        self, product_id: UUID, kind: str, source_hash: str, generator_key: str
    ) -> ImageCandidate | None:
        """The newest accepted row for this cache key (accepted rows are outside the index)."""
        stmt = (
            self._select()
            .where(
                ImageCandidate.product_id == _uuid("product_id", product_id),
                ImageCandidate.kind == kind,
                ImageCandidate.source_hash == source_hash,
                ImageCandidate.generator_key == generator_key,
                ImageCandidate.status == "accepted",
            )
            .order_by(ImageCandidate.decided_at.desc().nulls_last(), ImageCandidate.id.desc())
            .limit(1)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def latest_accepted_from(self, source_image_id: UUID, kind: str) -> ImageCandidate | None:
        """The newest accepted row of this kind made from this image (it was the main then)."""
        stmt = (
            self._select()
            .where(
                ImageCandidate.source_image_id == _uuid("source_image_id", source_image_id),
                ImageCandidate.kind == kind,
                ImageCandidate.status == "accepted",
            )
            .order_by(ImageCandidate.decided_at.desc().nulls_last(), ImageCandidate.id.desc())
            .limit(1)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def find_refused_today(
        self, product_id: UUID, kind: str, source_hash: str, generator_key: str, day: date
    ) -> ImageCandidate | None:
        """Today's `failed/budget` refusal for this cache key, if any: a refused create asked
        again answers it instead of writing another row."""
        start, end = day_bounds(day)
        stmt = (
            self._select()
            .where(
                ImageCandidate.product_id == _uuid("product_id", product_id),
                ImageCandidate.kind == kind,
                ImageCandidate.source_hash == source_hash,
                ImageCandidate.generator_key == generator_key,
                ImageCandidate.status == "failed",
                ImageCandidate.failure_reason == "budget",
                ImageCandidate.created_at >= start,
                ImageCandidate.created_at < end,
            )
            .order_by(ImageCandidate.created_at.desc())
            .limit(1)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def get(self, candidate_id: UUID) -> ImageCandidate | None:
        """The row as stored now (a row loaded earlier in the session is refreshed: the fenced
        writes are Core UPDATEs)."""
        stmt = (
            self._select()
            .where(ImageCandidate.id == _uuid("candidate_id", candidate_id))
            .execution_options(populate_existing=True)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def get_for_update(self, candidate_id: UUID) -> ImageCandidate | None:
        self._own_session()
        stmt = (
            self._select()
            .where(ImageCandidate.id == _uuid("candidate_id", candidate_id))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def list_for_product(
        self, product_id: UUID, *, limit: int = LIST_MAX
    ) -> builtins.list[ImageCandidate]:
        if not 1 <= limit <= LIST_MAX:
            raise ValueError(f"limit must be in 1..{LIST_MAX}")
        stmt = (
            self._select()
            .where(ImageCandidate.product_id == _uuid("product_id", product_id))
            .order_by(ImageCandidate.created_at.desc(), ImageCandidate.id.desc())
            .limit(limit)
            .execution_options(populate_existing=True)
        )
        return builtins.list((await self.db.execute(stmt)).scalars())

    async def count_ai_today(self, day: date, *, exclude_id: UUID | None = None) -> int:
        """PLAN D21: the tenant's AI candidates of the UTC day that are queued or reached the
        model (a budget refusal with no run is not counted), `exclude_id` aside. A call that may
        have been billed with no stored run (an untrusted result, a seam exception, a timeout)
        keeps its job's reservation, so a kept reservation counts too."""
        start, end = day_bounds(day)
        kept = exists(
            select(JobRun.id).where(
                JobRun.tenant_id == ImageCandidate.tenant_id,
                JobRun.id == ImageCandidate.job_id,
                JobRun.cost_reserved_usd > 0,
            )
        )
        stmt = (
            self._select()
            .where(
                ImageCandidate.kind == AI_KIND,
                ImageCandidate.created_at >= start,
                ImageCandidate.created_at < end,
                or_(
                    ImageCandidate.status == "queued",
                    ImageCandidate.agent_run_id.is_not(None),
                    kept,
                ),
            )
            .with_only_columns(func.count(ImageCandidate.id))
        )
        if exclude_id is not None:
            stmt = stmt.where(ImageCandidate.id != exclude_id)
        return int((await self.db.execute(stmt)).scalar_one())

    async def expired(self, older_than: datetime, limit: int) -> builtins.list[ImageCandidate]:
        """Candidates past retention: never an accepted one (provenance stays)."""
        stmt = (
            self._select()
            .where(ImageCandidate.status != "accepted", ImageCandidate.created_at < older_than)
            .order_by(ImageCandidate.created_at)
            .limit(limit)
        )
        return builtins.list((await self.db.execute(stmt)).scalars())

    # ---- job-fenced writes ----

    async def set_job(self, candidate_id: UUID, job_id: UUID) -> bool:
        """Point a row with no job at `job_id`; False when it already has one."""
        stmt = self._fenced(candidate_id, ImageCandidate.job_id.is_(None)).values(
            job_id=_uuid("job_id", job_id)
        )
        return await self._write(stmt)

    async def record_run(
        self,
        candidate_id: UUID,
        job_id: UUID,
        *,
        agent_run_id: UUID,
        cost_usd: Decimal,
        served_provider: str | None,
    ) -> bool:
        stmt = self._fenced(candidate_id, ImageCandidate.job_id == job_id).values(
            agent_run_id=_uuid("agent_run_id", agent_run_id),
            cost_usd=cost_usd,
            served_provider=served_provider,
        )
        return await self._write(stmt)

    async def mark_ready(
        self,
        candidate_id: UUID,
        job_id: UUID,
        *,
        storage_key: str,
        width: int,
        height: int,
        fidelity: Mapping[str, Any] | None = None,
    ) -> bool:
        values: dict[str, Any] = {
            "status": "ready",
            "storage_key": storage_key,
            "mime_type": "image/jpeg",
            "width_px": width,
            "height_px": height,
        }
        if fidelity is not None:  # never a JSON `null`: the column is an object or SQL NULL
            values["fidelity_json"] = dict(fidelity)
        stmt = self._fenced(
            candidate_id, ImageCandidate.job_id == job_id, ImageCandidate.status == "queued"
        ).values(**values)
        return await self._write(stmt)

    async def mark_failed(
        self,
        candidate_id: UUID,
        job_id: UUID,
        reason: str,
        *,
        fidelity: Mapping[str, Any] | None = None,
    ) -> bool:
        values: dict[str, Any] = {"status": "failed", "failure_reason": reason}
        if fidelity is not None:
            values["fidelity_json"] = dict(fidelity)
        stmt = self._fenced(
            candidate_id, ImageCandidate.job_id == job_id, ImageCandidate.status == "queued"
        ).values(**values)
        return await self._write(stmt)

    # ---- the seller's decisions ----

    async def reject(self, candidate_id: UUID, user_id: UUID | None) -> bool:
        stmt = self._fenced(candidate_id, ImageCandidate.status == "ready").values(
            status="rejected", decided_by=user_id, decided_at=func.now()
        )
        return await self._write(stmt)

    async def reopen(self, candidate_id: UUID) -> bool:
        """A rejected candidate asked for again is ready again (no new job, PLAN D10)."""
        stmt = self._fenced(candidate_id, ImageCandidate.status == "rejected").values(
            status="ready", decided_by=None, decided_at=None
        )
        return await self._write(stmt)

    async def mark_accepted(self, candidate_id: UUID, image_id: UUID, user_id: UUID | None) -> bool:
        stmt = self._fenced(candidate_id, ImageCandidate.status == "ready").values(
            status="accepted",
            accepted_image_id=_uuid("image_id", image_id),
            decided_by=user_id,
            decided_at=func.now(),
        )
        return await self._write(stmt)

    async def fail_for_source(self, source_image_id: UUID) -> builtins.list[str]:
        """A deleted or replaced source: its open candidates become `failed/source_changed`
        (outside the cache index, so the same photo asked for again is a new candidate). Returns
        the object keys they held, for the caller to delete after the commit."""
        return await self._retire(
            ImageCandidate.source_image_id == _uuid("source_image_id", source_image_id)
        )

    async def ids_for_products(self, product_ids: Sequence[UUID]) -> builtins.list[UUID]:
        """Every candidate id of these products that is not accepted (product delete: their
        jobs are found by these ids)."""
        if not product_ids:
            return []
        stmt = (
            self._select()
            .where(
                ImageCandidate.product_id.in_(list(product_ids)),
                ImageCandidate.status != "accepted",
            )
            .with_only_columns(ImageCandidate.id)
            .order_by(ImageCandidate.id)
        )
        return builtins.list((await self.db.execute(stmt)).scalars())

    async def retire_for_products(self, product_ids: Sequence[UUID]) -> builtins.list[str]:
        """Product delete: the products' open candidates become `failed/source_changed`, as
        `fail_for_source` does. Returns the object keys they held; the caller leaves them to the
        30-day candidate sweep, which deletes by the candidate key."""
        if not product_ids:
            return []
        return await self._retire(ImageCandidate.product_id.in_(list(product_ids)))

    async def retire(self, candidate_id: UUID) -> builtins.list[str]:
        """One open candidate whose source is no longer its live main: `failed/source_changed`,
        as `fail_for_source` does. Returns the object key it held, if any."""
        return await self._retire(ImageCandidate.id == _uuid("candidate_id", candidate_id))

    async def _retire(self, match: Any) -> builtins.list[str]:
        self._own_session()
        stmt = self._scoped_write(
            update(ImageCandidate)
            .where(
                match,
                ImageCandidate.status.in_(_DECIDABLE),
                self._tenant_criterion(include_platform=False),
            )
            .values(status="failed", failure_reason="source_changed", storage_key=None)
            .returning(ImageCandidate.id)
        )
        before = (
            self._select()
            .where(
                match,
                ImageCandidate.status.in_(_DECIDABLE),
                ImageCandidate.storage_key.is_not(None),
            )
            .with_only_columns(ImageCandidate.storage_key)
        )
        keys = [k for k in (await self.db.execute(before)).scalars() if k]
        await self.db.execute(stmt)
        return keys

    async def delete(self, candidate_id: UUID) -> bool:
        self._own_session()
        stmt = self._scoped_write(
            delete(ImageCandidate)
            .where(
                ImageCandidate.id == _uuid("candidate_id", candidate_id),
                ImageCandidate.status != "accepted",
                self._tenant_criterion(include_platform=False),
            )
            .returning(ImageCandidate.id)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none() is not None
