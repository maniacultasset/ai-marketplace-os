"""`imports` and `import_rows` (DATABASE.md:954-976; S13 B8, B9, B13, B14; Q10, Q12).

`ImportRepository`: the per-tenant live-import lock and count (PD13, Q10: an advisory
transaction lock on `bulk_import:<tenant_id>`, held to COMMIT, so the cap check and the job
write of two concurrent uploads, or two concurrent rows retries, serialise), the `imports` row,
and the job-mirroring writes, each fenced on the job that owns the import (`WHERE job_id = :job`),
so an old job's hook never overwrites a newer job's state.

`ImportRowRepository`: one `import_rows` row per CSV row, written as `imported` or `failed` only
(never `pending`), idempotent on `(tenant_id, import_id, row_number)`; the finished row numbers a
resumed run skips; the totals; the keyset page for the API.
"""

from __future__ import annotations

import builtins
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import func, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import defer

from app.db.guard import TENANT_INFO_KEY, CrossTenantQueryError
from app.db.models.ops import Import, ImportRow, JobRun
from app.repositories.base import TenantScopedRepository

LIVE_JOB_STATES: Final = ("queued", "running")
ROW_STATES: Final = ("imported", "failed")
ROWS_PAGE_MAX: Final = 200

_LOCK: Final = text("SELECT pg_advisory_xact_lock(hashtextextended('bulk_import:' || :t, 0))")


class ImportRepository(TenantScopedRepository[Import]):
    model = Import

    def _own_session(self) -> None:
        if self.db.info.get(TENANT_INFO_KEY) != self.tenant_id:
            raise CrossTenantQueryError("imports write for another tenant's unit of work")

    async def lock_tenant_imports(self) -> None:
        """The tenant's import lock, held until the transaction ends (Q10)."""
        self._own_session()
        await self.db.execute(_LOCK, {"t": str(self.tenant_id)})

    async def count_live_bulk_imports(self) -> int:
        """The tenant's `bulk_import` jobs that are `queued` or `running` (PD13)."""
        return await _TenantJobs(self.db, self.tenant_id).count_live("bulk_import")

    async def create(
        self,
        *,
        import_id: uuid.UUID,
        storage_key: str,
        filename: str,
        job_id: uuid.UUID,
        created_by: uuid.UUID,
    ) -> Import:
        row = Import(
            id=import_id,
            storage_key=storage_key,
            filename=filename,
            status="queued",
            total_rows=None,
            imported_rows=0,
            failed_rows=0,
            job_id=job_id,
            created_by=created_by,
        )
        return await self.add(row)

    async def get(self, import_id: uuid.UUID) -> Import | None:
        return await self.get_by_id(import_id)

    async def get_for_job(self, import_id: uuid.UUID, job_id: uuid.UUID) -> Import | None:
        """The import while `job_id` still owns it (the fence of every job-side write)."""
        stmt = self._select().where(Import.id == import_id, Import.job_id == job_id)
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def _fenced(
        self, import_id: uuid.UUID, job_id: uuid.UUID, values: Mapping[str, Any]
    ) -> bool:
        self._own_session()
        stmt = self._scoped_write(
            update(Import)
            .where(
                Import.id == import_id,
                Import.job_id == job_id,
                self._tenant_criterion(include_platform=False),
            )
            .values(**values)
        )
        result = await self.db.execute(stmt)
        return int(getattr(result, "rowcount", 0) or 0) == 1

    async def mark_running(self, import_id: uuid.UUID, job_id: uuid.UUID) -> bool:
        return await self._fenced(import_id, job_id, {"status": "running", "finished_at": None})

    async def finish(
        self,
        import_id: uuid.UUID,
        job_id: uuid.UUID,
        *,
        status: str,
        totals: ImportTotals | None,
        error_report_json: dict[str, Any] | None = None,
    ) -> bool:
        """The job's terminal state mirrored onto the import (`dead_letter` → `failed`)."""
        values: dict[str, Any] = {
            "status": "failed" if status == "dead_letter" else status,
            "finished_at": func.now(),
        }
        if totals is not None:
            values.update(
                total_rows=totals.total, imported_rows=totals.imported, failed_rows=totals.failed
            )
        if error_report_json is not None:
            values["error_report_json"] = error_report_json
        return await self._fenced(import_id, job_id, values)

    async def hand_to_job(
        self, import_id: uuid.UUID, old_job_id: uuid.UUID, new_job_id: uuid.UUID
    ) -> bool:
        """A new job (a requeue or a rows retry) takes the import over: `job_id` moves to it and
        the status goes back to `queued`, only while `old_job_id` still owns it."""
        return await self._fenced(
            import_id, old_job_id, {"job_id": new_job_id, "status": "queued", "finished_at": None}
        )


class _TenantJobs(TenantScopedRepository[JobRun]):
    model = JobRun

    async def count_live(self, kind: str) -> int:
        stmt = (
            self._select()
            .where(JobRun.kind == kind, JobRun.status.in_(LIVE_JOB_STATES))
            .with_only_columns(func.count(JobRun.id))
        )
        return int((await self.db.execute(stmt)).scalar_one())


@dataclass(frozen=True, slots=True)
class ImportTotals:
    total: int
    imported: int
    failed: int


@dataclass(frozen=True, slots=True)
class RowWrite:
    row_number: int
    status: str
    raw_row: dict[str, Any]
    sku: str | None = None
    parent_sku: str | None = None
    product_id: uuid.UUID | None = None
    error_report_json: dict[str, Any] | None = None


class ImportRowRepository(TenantScopedRepository[ImportRow]):
    model = ImportRow

    def _own_session(self) -> None:
        if self.db.info.get(TENANT_INFO_KEY) != self.tenant_id:
            raise CrossTenantQueryError("import_rows write for another tenant's unit of work")

    async def write(self, import_id: uuid.UUID, job_id: uuid.UUID, row: RowWrite) -> None:
        """Insert or overwrite the row's result (idempotent on its row number)."""
        self._own_session()
        if row.status not in ROW_STATES:
            raise ValueError("an import row is written only as imported or failed")
        values = {
            "tenant_id": self.tenant_id,
            "import_id": import_id,
            "row_number": row.row_number,
            "sku": row.sku,
            "parent_sku": row.parent_sku,
            "raw_row": row.raw_row,
            "status": row.status,
            "product_id": row.product_id,
            "error_report_json": row.error_report_json,
            "job_id": job_id,
        }
        stmt = pg_insert(ImportRow).values(**values)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_import_rows_tenant_id_import_id_row_number",
            set_={
                name: stmt.excluded[name]
                for name in ("sku", "parent_sku", "status", "product_id", "job_id")
            }
            | {"error_report_json": stmt.excluded.error_report_json},
        )
        await self.db.execute(stmt)

    async def finished_row_numbers(self, import_id: uuid.UUID) -> set[int]:
        stmt = (
            self._select()
            .where(ImportRow.import_id == import_id, ImportRow.status.in_(ROW_STATES))
            .with_only_columns(ImportRow.row_number)
        )
        return {int(n) for n in (await self.db.execute(stmt)).scalars()}

    async def failed_rows(self, import_id: uuid.UUID) -> builtins.list[ImportRow]:
        stmt = (
            self._select()
            .where(ImportRow.import_id == import_id, ImportRow.status == "failed")
            .order_by(ImportRow.row_number)
        )
        return builtins.list((await self.db.execute(stmt)).scalars())

    async def totals(self, import_id: uuid.UUID) -> ImportTotals:
        stmt = (
            self._select()
            .where(ImportRow.import_id == import_id, ImportRow.status.in_(ROW_STATES))
            .with_only_columns(ImportRow.status)
        )
        statuses = builtins.list((await self.db.execute(stmt)).scalars())
        imported = sum(1 for s in statuses if s == "imported")
        failed = sum(1 for s in statuses if s == "failed")
        return ImportTotals(total=imported + failed, imported=imported, failed=failed)

    async def page(
        self, import_id: uuid.UUID, *, status: str | None, after: int | None, limit: int
    ) -> tuple[builtins.list[ImportRow], bool]:
        """Keyset on `row_number` (index DATABASE.md:976); `raw_row` is never read."""
        if isinstance(limit, bool) or not 1 <= limit <= ROWS_PAGE_MAX:
            raise ValueError(f"limit must be in 1..{ROWS_PAGE_MAX}")
        states = ROW_STATES if status is None else (status,)
        if not set(states) <= set(ROW_STATES):
            raise ValueError("status must be imported or failed")
        stmt = (
            self._select()
            .where(ImportRow.import_id == import_id, ImportRow.status.in_(states))
            .options(defer(ImportRow.raw_row))
            .order_by(ImportRow.row_number)
            .limit(limit + 1)
        )
        if after is not None:
            stmt = stmt.where(ImportRow.row_number > after)
        rows = builtins.list((await self.db.execute(stmt)).scalars())
        return rows[:limit], len(rows) > limit
