"""`job_runs` (DATABASE.md §5.4 :928-950; ADR-0004): the one enqueue helper and the state moves of
the job spine. Every write names the tenant and, for a running job, fences on the claimed
`attempt`, so a worker whose lease was reaped can never overwrite the newer attempt's row.

- `enqueue`: INSERT the row and defer its queue entry in the caller's transaction (DW1); a
  repeated `idempotency_key` returns the existing row and defers nothing.
- `claim`: the DATABASE.md:1027 compare-and-set, also requiring a running tenant (PLAN D14).
- `finish`, `release_retry`, `release_shutdown`: the wrapper's terminal write and its two
  releases, all fenced on `status = 'running' AND attempt = :claimed`.
- `cancel_queued`, `cancel_non_running`, `reap`, `requeue`: the API cancel, the non-running
  tenant cancel, the reaper and the staff requeue.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final

from mkos_schemas.enums import JobKind
from sqlalchemy import exists, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.sql.elements import ColumnElement

from app.db.guard import TENANT_INFO_KEY, CrossTenantQueryError
from app.db.models.ops import JobRun
from app.db.models.tenancy import Tenant
from app.db.queue import cancel_on_session, defer_on_session, retire_on_session
from app.repositories.base import TenantScopedRepository
from app.workers.policy import POLICIES

MAX_PAYLOAD_BYTES: Final = 64 * 1024  # DATABASE.md:941
RUNNING_TENANT: Final = ("trial", "active")
TERMINAL: Final = frozenset({"succeeded", "partial", "failed", "cancelled", "dead_letter"})
ENTITY_TYPES: Final = frozenset({"listing", "import", "image", "brand_document"})


def job_idempotency_key(http_key: str, kind: str, listing_id: uuid.UUID | None) -> str:
    """DATABASE.md:940: `hex(sha256(http_key ':' kind ':' listing_id))`, one key per batch item."""
    raw = f"{http_key}:{kind}:{listing_id or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class EnqueueSpec:
    kind: JobKind
    entity_type: str
    entity_id: uuid.UUID
    created_by: uuid.UUID | None
    listing_id: uuid.UUID | None = None
    http_key: str | None = None  # batch items only; single-job endpoints pass None
    payload: dict[str, Any] | None = None
    batch_id: uuid.UUID | None = None
    parent_job_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class Enqueued:
    job: JobRun
    created: bool


@dataclass(frozen=True, slots=True)
class Claimed:
    job: JobRun
    attempt: int


@dataclass(frozen=True, slots=True)
class Reaped:
    """What the reaper did to one job: `requeued`, `dead_letter`, `cancelled` or `skipped`
    (another reaper holds it, or it is no longer an expired `running` row)."""

    action: str
    job: JobRun | None = None


async def cancel_queue_entry(session: Any, job: JobRun) -> bool:
    """Cancel the job's `todo` queue entry in the session's transaction (a no-op without one).
    The row is already `cancelled`, so a delivery that races this is dropped by the claim CAS."""
    if job.queue_job_id is None:
        return False
    return await cancel_on_session(session, job.queue_job_id)


async def expired_running(session: Any, *, limit: int = 500) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """`(id, tenant_id)` of every `running` row whose lease has expired, oldest lease first.
    Cross-tenant: run it only in `platform_scope()` (the reaper, DATABASE.md:1028)."""
    stmt = (
        select(JobRun.id, JobRun.tenant_id)
        .where(JobRun.status == "running", JobRun.lease_expires_at < func.now())
        .order_by(JobRun.lease_expires_at)
        .limit(limit)
    )
    return [(row.id, row.tenant_id) for row in (await session.execute(stmt)).all()]


def _tenant_running() -> ColumnElement[bool]:
    """The job's tenant is `trial` or `active` (PLAN D14), correlated on `job_runs.tenant_id`."""
    return exists(
        select(Tenant.id).where(Tenant.id == JobRun.tenant_id, Tenant.status.in_(RUNNING_TENANT))
    )


def _payload_bytes(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))


class JobRunRepository(TenantScopedRepository[JobRun]):
    model = JobRun

    def _own_session(self) -> None:
        """Core DML skips the flush guard, so name the session's tenant here: a repository
        for tenant A inside tenant B's unit of work is refused before any statement."""
        if self.db.info.get(TENANT_INFO_KEY) != self.tenant_id:
            raise CrossTenantQueryError("job_runs write for another tenant's unit of work")

    def _where(self, job_id: uuid.UUID, *extra: ColumnElement[bool]) -> list[ColumnElement[bool]]:
        if not isinstance(job_id, uuid.UUID):
            raise ValueError("job_id must be a UUID")
        return [JobRun.id == job_id, self._tenant_criterion(include_platform=False), *extra]

    async def _update_returning(
        self, job_id: uuid.UUID, where: list[ColumnElement[bool]], values: dict[str, Any]
    ) -> JobRun | None:
        self._own_session()
        stmt = self._scoped_write(update(JobRun).where(*where).values(**values)).returning(JobRun)
        row = (
            await self.db.execute(stmt, execution_options={"populate_existing": True})
        ).scalar_one_or_none()
        return row

    # ---- enqueue (DW1, AC1-2) ----

    async def enqueue(self, spec: EnqueueSpec) -> Enqueued:
        self._own_session()
        kind = JobKind(spec.kind)
        if spec.entity_type not in ENTITY_TYPES:
            raise ValueError(f"unknown entity_type {spec.entity_type!r}")
        payload = dict(spec.payload or {})
        if _payload_bytes(payload) > MAX_PAYLOAD_BYTES:
            raise ValueError(f"payload exceeds {MAX_PAYLOAD_BYTES} bytes")
        key = (
            job_idempotency_key(spec.http_key, kind.value, spec.listing_id)
            if spec.http_key is not None
            else None
        )
        values: dict[str, Any] = {
            "tenant_id": self.tenant_id,
            "kind": kind.value,
            "entity_type": spec.entity_type,
            "entity_id": spec.entity_id,
            "listing_id": spec.listing_id,
            "idempotency_key": key,
            "payload_json": payload,
            "correlation_id": uuid.uuid4(),
            "parent_job_id": spec.parent_job_id,
            "created_by": spec.created_by,
            "batch_id": spec.batch_id,
            "max_attempts": POLICIES[kind].max_attempts,
        }
        insert = pg_insert(JobRun).values(**values)
        if key is not None:
            insert = insert.on_conflict_do_nothing(
                index_elements=[JobRun.tenant_id, JobRun.idempotency_key],
                index_where=JobRun.idempotency_key.is_not(None),
            )
        stmt = insert.returning(JobRun)
        job = (await self.db.execute(stmt)).scalar_one_or_none()
        if job is None:
            existing = await self.db.execute(self._select().where(JobRun.idempotency_key == key))
            return Enqueued(existing.scalar_one(), created=False)
        queue_job_id = await defer_on_session(
            self.db, kind=kind.value, queue=job.queue, job_run_id=job.id
        )
        await self.db.execute(
            self._scoped_write(
                update(JobRun).where(*self._where(job.id)).values(queue_job_id=queue_job_id)
            )
        )
        set_committed_value(job, "queue_job_id", queue_job_id)
        return Enqueued(job, created=True)

    # ---- the worker's claim and terminal writes (A5, A6) ----

    async def claim(
        self, job_id: uuid.UUID, lease_s: int, *, request_id: str | None = None
    ) -> Claimed | None:
        """`queued → running`, `attempt + 1`, a lease of `lease_s` seconds, only while the
        tenant is `trial` or `active`; a row with no `request_id` takes the worker's (so the
        job envelope always names one). None: a duplicate or stale delivery, or a stopped
        tenant."""
        job = await self._update_returning(
            job_id,
            self._where(
                job_id,
                JobRun.status == "queued",
                JobRun.attempt < JobRun.max_attempts,
                _tenant_running(),
            ),
            {
                "status": "running",
                "attempt": JobRun.attempt + 1,
                "started_at": func.now(),
                "lease_expires_at": func.now() + timedelta(seconds=lease_s),
                "request_id": func.coalesce(JobRun.request_id, request_id),
            },
        )
        return None if job is None else Claimed(job, job.attempt)

    async def cancel_non_running(self, job_id: uuid.UUID) -> JobRun | None:
        """A queued job of a tenant that is not `trial|active` → `cancelled` (PLAN D14)."""
        return await self._update_returning(
            job_id,
            self._where(job_id, JobRun.status == "queued", ~_tenant_running()),
            {"status": "cancelled", "finished_at": func.now()},
        )

    def _fence(self, job_id: uuid.UUID, attempt: int) -> list[ColumnElement[bool]]:
        return self._where(job_id, JobRun.status == "running", JobRun.attempt == attempt)

    async def finish(
        self,
        job_id: uuid.UUID,
        attempt: int,
        *,
        status: str,
        result_json: dict[str, Any] | None = None,
        error_report_json: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> JobRun | None:
        """The terminal write of the claimed attempt; None when the fence drops it."""
        if status not in TERMINAL:
            raise ValueError(f"{status!r} is not a terminal status")
        return await self._update_returning(
            job_id,
            self._fence(job_id, attempt),
            {
                "status": status,
                "result_json": result_json,
                "error_report_json": error_report_json,
                "error_code": error_code,
                "lease_expires_at": None,
                "finished_at": func.now(),
            },
        )

    async def release_retry(self, job_id: uuid.UUID, attempt: int) -> bool:
        """A retryable failure with attempts left: back to `queued`, the attempt kept."""
        row = await self._update_returning(
            job_id,
            self._fence(job_id, attempt),
            {"status": "queued", "lease_expires_at": None, "started_at": None},
        )
        return row is not None

    async def release_shutdown(self, job_id: uuid.UUID, attempt: int) -> bool:
        """A shutdown abort is not an attempt (s8 D6): back to `queued`, `attempt - 1`."""
        row = await self._update_returning(
            job_id,
            self._fence(job_id, attempt),
            {
                "status": "queued",
                "attempt": JobRun.attempt - 1,
                "lease_expires_at": None,
                "started_at": None,
            },
        )
        return row is not None

    async def set_result(self, job_id: uuid.UUID, attempt: int, result: dict[str, Any]) -> bool:
        """Progress on a running attempt (`result_json`, e.g. import progress); fenced."""
        row = await self._update_returning(
            job_id, self._fence(job_id, attempt), {"result_json": result}
        )
        return row is not None

    # ---- API cancel (A14) ----

    async def cancel_queued(self, job_id: uuid.UUID) -> JobRun | None:
        """`queued → cancelled`; None when the job is not queued (any other status is 409)."""
        return await self._update_returning(
            job_id,
            self._where(job_id, JobRun.status == "queued"),
            {"status": "cancelled", "finished_at": func.now()},
        )

    # ---- the reaper (A9; DATABASE.md:1028) ----

    async def reap(self, job_id: uuid.UUID, report_json: Any) -> Reaped:
        """One expired `running` row, locked `FOR UPDATE SKIP LOCKED` (a concurrent reaper skips
        it): a stopped tenant's job is `cancelled`; with attempts left it goes back to `queued`
        and is re-deferred (a new queue entry); exhausted, it is `dead_letter` with
        `report_json(job)` (`JOB_TIMEOUT`). The dead entry is retired in every case, so it can
        never run. A reaped attempt counts (s8 D6): the next claim takes `attempt + 1`."""
        self._own_session()
        locked = await self.db.execute(
            self._select()
            .where(
                JobRun.id == job_id,
                JobRun.status == "running",
                JobRun.lease_expires_at < func.now(),
            )
            .with_for_update(skip_locked=True)
        )
        job = locked.scalar_one_or_none()
        if job is None:
            return Reaped("skipped")
        if job.queue_job_id is not None:
            await retire_on_session(self.db, job.queue_job_id)
        fence = self._fence(job_id, job.attempt)
        stopped = await self._update_returning(
            job_id,
            [*fence, ~_tenant_running()],
            {"status": "cancelled", "lease_expires_at": None, "finished_at": func.now()},
        )
        if stopped is not None:
            return Reaped("cancelled", stopped)
        if job.attempt < job.max_attempts:
            done = await self._update_returning(
                job_id, fence, {"status": "queued", "lease_expires_at": None, "started_at": None}
            )
            if done is None:
                return Reaped("skipped")
            queue_job_id = await defer_on_session(
                self.db, kind=done.kind, queue=done.queue, job_run_id=done.id
            )
            await self.db.execute(
                self._scoped_write(
                    update(JobRun).where(*self._where(job_id)).values(queue_job_id=queue_job_id)
                )
            )
            set_committed_value(done, "queue_job_id", queue_job_id)
            return Reaped("requeued", done)
        report = report_json(job)
        done = await self._update_returning(
            job_id,
            fence,
            {
                "status": "dead_letter",
                "lease_expires_at": None,
                "finished_at": func.now(),
                "error_report_json": report,
                "error_code": report.get("error_code"),
            },
        )
        return Reaped("dead_letter", done)
