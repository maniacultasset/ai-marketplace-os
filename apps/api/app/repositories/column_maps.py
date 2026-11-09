"""`import_column_maps` (0027; SI14 T16-T18): a seller's header row mapped onto the importer's
accepted names. Tenant-scoped (FORCE RLS); app_rw holds no DELETE, so a map lives until the tenant
purge. `samples` are seller cells (the create path stores none until a seller read exists):
cleared at confirm, by the 7-day retention step, never sent to a model and never selected by a
read that leaves this tenant."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import update

from app.db.models.ops import ImportColumnMap
from app.repositories.base import TenantScopedRepository


class ImportColumnMapRepository(TenantScopedRepository[ImportColumnMap]):
    model = ImportColumnMap

    async def create(
        self,
        *,
        map_id: uuid.UUID,
        status: str,
        headers: Sequence[str],
        headers_digest: str,
        mappings: Sequence[Mapping[str, Any]],
        samples: Mapping[str, Any] | None,
        created_by: uuid.UUID,
        job_id: uuid.UUID | None = None,
    ) -> ImportColumnMap:
        row = ImportColumnMap(
            id=map_id,
            status=status,
            headers=list(headers),
            headers_digest=headers_digest,
            mappings=[dict(m) for m in mappings],
            samples=None if samples is None else dict(samples),
            job_id=job_id,
            created_by_user_id=created_by,
        )
        return await self.add(row)

    async def get(self, map_id: uuid.UUID) -> ImportColumnMap | None:
        return await self.get_by_id(map_id)

    async def get_for_update(self, map_id: uuid.UUID) -> ImportColumnMap | None:
        stmt = (
            self._select()
            .where(ImportColumnMap.id == map_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        found: ImportColumnMap | None = (await self.db.execute(stmt)).scalar_one_or_none()
        return found

    async def set_job(self, map_id: uuid.UUID, job_id: uuid.UUID) -> None:
        await self.db.execute(
            self._scoped_write(
                update(ImportColumnMap)
                .where(self._tenant_criterion(include_platform=False), ImportColumnMap.id == map_id)
                .values(job_id=job_id)
            )
        )

    async def set_result(
        self, map_id: uuid.UUID, *, status: str, mappings: Sequence[Mapping[str, Any]]
    ) -> bool:
        """The job's answer (or the settle of a dead job), only while the map is `mapping`."""
        stmt = self._scoped_write(
            update(ImportColumnMap)
            .where(
                self._tenant_criterion(include_platform=False),
                ImportColumnMap.id == map_id,
                ImportColumnMap.status == "mapping",
            )
            .values(status=status, mappings=[dict(m) for m in mappings])
        )
        return int(getattr(await self.db.execute(stmt), "rowcount", 0) or 0) == 1

    async def confirm(
        self, map_id: uuid.UUID, *, mappings: Sequence[Mapping[str, Any]], at: datetime
    ) -> bool:
        """The seller's answers: `confirmed`, the samples cleared (only from `ready` or
        `failed`)."""
        stmt = self._scoped_write(
            update(ImportColumnMap)
            .where(
                self._tenant_criterion(include_platform=False),
                ImportColumnMap.id == map_id,
                ImportColumnMap.status.in_(("ready", "failed")),
            )
            .values(
                status="confirmed",
                mappings=[dict(m) for m in mappings],
                samples=None,
                confirmed_at=at,
            )
        )
        return int(getattr(await self.db.execute(stmt), "rowcount", 0) or 0) == 1


__all__ = ["ImportColumnMapRepository"]
