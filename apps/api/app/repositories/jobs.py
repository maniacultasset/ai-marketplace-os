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
from sqlalchemy import exists, func, null, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.sql.elements import ColumnElement

from app.db.guard import TENANT_INFO_KEY, CrossTenantQueryError
from app.db.integrity import map_db_error, to_conflict
from app.db.models.ops import JobRun
from app.db.models.tenancy import Tenant
from app.db.queue import cancel_on_session, defer_on_session, retire_on_session
from app.repositories.base import TenantScopedRepository
from app.workers.policy import POLICIES, lock_for

MAX_PAYLOAD_BYTES: Final = 64 * 1024  # DATABASE.md:941
RUNNING_TENANT: Final = ("trial", "active")
TERMINAL: Final = frozenset({"succeeded", "partial", "failed", "cancelled", "dead_letter"})
LIVE: Final = ("queued", "running")
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
    max_attempts: int | None = None  # a requeue copies its parent's; else the kind's policy
    # S17b (PLAN D15(b)): a procrastinate lock; jobs sharing one never run at the same time,
    # and a waiting one stays unfetched (`todo`), holding no worker slot
    lock: str | None = None


@dataclass(frozen=True, slots=True)
class Enqueued:
    job: JobRun
    created: bool


@dataclass(frozen=True, slots=True)
class Claimed:
    job: JobRun
    attempt: int


class NotEligibleError(Exception):
    """A staff requeue refused: unknown job, not `dead_letter`/`failed`, already requeued, or a
    tenant that is not `trial|active`. The message is a fixed phrase, never row data."""


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


async def job_tenant(session: Any, job_id: uuid.UUID) -> uuid.UUID | None:
    """The tenant of job `job_id`, or None. Cross-tenant: run it only in `platform_scope()`."""
    stmt = select(JobRun.tenant_id).where(JobRun.id == job_id)
    found: uuid.UUID | None = (await session.execute(stmt)).scalar_one_or_none()
    return found


@dataclass(frozen=True, slots=True)
class DeadLetter:
    job_id: uuid.UUID
    tenant_slug: str
    kind: str
    status: str
    attempt: int
    error_code: str | None
    finished_at: Any


async def list_dead_letters(
    session: Any, *, tenant_slug: str | None = None, limit: int = 100
) -> list[DeadLetter]:
    """The newest dead letters, ids and codes only (never payloads or reports). Cross-tenant:
    run it only in `platform_scope()` (the staff CLI, DATABASE.md:312)."""
    if isinstance(limit, bool) or not 1 <= limit <= 500:
        raise ValueError("limit must be in 1..500")
    stmt = (
        select(
            JobRun.id,
            Tenant.slug,
            JobRun.kind,
            JobRun.status,
            JobRun.attempt,
            JobRun.error_code,
            JobRun.finished_at,
        )
        .join(Tenant, Tenant.id == JobRun.tenant_id)
        .where(JobRun.status == "dead_letter")
        .order_by(JobRun.finished_at.desc(), JobRun.id)
        .limit(limit)
    )
    if tenant_slug is not None:
        stmt = stmt.where(Tenant.slug == tenant_slug)
    return [DeadLetter(*row) for row in (await session.execute(stmt)).all()]


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


# The orphan sweep's read of the queue's own table (raw SQL, as every queue-table statement here:
# `procrastinate_jobs` has no tenant column; the ids come from this tenant's own rows).
_ORPHAN_ENTRIES: Final = text(
    "SELECT id FROM procrastinate_jobs WHERE id = ANY(:ids) AND status = 'doing' "
    "AND worker_id IS NULL"
)


async def tenants_with_queued(session: Any, *, limit: int = 500) -> list[uuid.UUID]:
    """Every tenant holding a `queued` row, for the orphan sweep (KNOWN-ISSUES §E, S13a).
    Cross-tenant: only in `platform_scope()` (the reaper). The queue table itself is read in
    each tenant's own unit of work (`JobRunRepository.orphaned_queued`)."""
    stmt = select(JobRun.tenant_id).where(JobRun.status == "queued").distinct().limit(limit)
    return [row.tenant_id for row in (await session.execute(stmt)).all()]


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

    async def latest_for_entity(self, entity_type: str, entity_id: uuid.UUID) -> JobRun | None:
        """The newest job (by `queued_at`, then id) on one entity of this tenant (S17b D8)."""
        if not isinstance(entity_id, uuid.UUID):
            raise ValueError("entity_id must be a UUID")
        stmt = (
            self._select()
            .where(JobRun.entity_type == entity_type, JobRun.entity_id == entity_id)
            .order_by(JobRun.queued_at.desc(), JobRun.id.desc())
            .limit(1)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

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
            "max_attempts": (
                POLICIES[kind].max_attempts if spec.max_attempts is None else spec.max_attempts
            ),
        }
        insert = pg_insert(JobRun).values(**values)
        if key is not None:
            insert = insert.on_conflict_do_nothing(
                index_elements=[JobRun.tenant_id, JobRun.idempotency_key],
                index_where=JobRun.idempotency_key.is_not(None),
            )
        stmt = insert.returning(JobRun)
        try:
            job = (await self.db.execute(stmt)).scalar_one_or_none()
        except IntegrityError as exc:  # S21: ux_job_runs_live → 409 LISTING_BUSY
            raise to_conflict(exc) from exc
        except DBAPIError as exc:  # S21: a 0004 guard (the publish gate) → its DomainError
            mapped = map_db_error(exc)
            if mapped is not None:
                raise mapped from exc
            raise
        if job is None:
            existing = await self.db.execute(self._select().where(JobRun.idempotency_key == key))
            return Enqueued(existing.scalar_one(), created=False)
        queue_job_id = await defer_on_session(
            self.db,
            kind=kind.value,
            queue=job.queue,
            job_run_id=job.id,
            lock=spec.lock or lock_for(kind),
        )
        await self.db.execute(
            self._scoped_write(
                update(JobRun).where(*self._where(job.id)).values(queue_job_id=queue_job_id)
            )
        )
        set_committed_value(job, "queue_job_id", queue_job_id)
        return Enqueued(job, created=True)

    # ---- S21 status-sync helpers (CP4) ----

    async def live_for_listing(self, listing_id: uuid.UUID, kind: JobKind) -> JobRun | None:
        """The listing's live (`queued`/`running`) job of `kind`, if any."""
        stmt = self._select().where(
            JobRun.listing_id == listing_id,
            JobRun.kind == JobKind(kind).value,
            JobRun.status.in_(LIVE),
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def user_requested_since(
        self, listing_id: uuid.UUID, kind: JobKind, seconds: int
    ) -> bool:
        """A job of `kind` a member requested (`created_by` set) in the last `seconds`."""
        stmt = (
            self._select()
            .where(
                JobRun.listing_id == listing_id,
                JobRun.kind == JobKind(kind).value,
                JobRun.created_by.is_not(None),
                JobRun.queued_at > func.now() - timedelta(seconds=seconds),
            )
            .with_only_columns(JobRun.id)
            .limit(1)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none() is not None

    # ---- S16 brand_embed helpers (T9; CP6) ----

    async def count_live(self, kind: JobKind, *, source: str) -> int:
        """Live (`queued`/`running`) jobs of `kind` whose payload `source` is `source`."""
        stmt = (
            self._select()
            .where(
                JobRun.kind == JobKind(kind).value,
                JobRun.status.in_(LIVE),
                JobRun.payload_json["source"].astext == source,
            )
            .with_only_columns(func.count())
        )
        return int((await self.db.execute(stmt)).scalar_one())

    async def has_queued(self, kind: JobKind, *, entity_id: uuid.UUID, source: str) -> bool:
        """A `queued` job of `kind` for `entity_id` and payload `source`, row-locked (FOR UPDATE)
        so a worker's claim CAS on it waits for the caller's transaction (CP6)."""
        stmt = (
            self._select()
            .where(
                JobRun.kind == JobKind(kind).value,
                JobRun.status == "queued",
                JobRun.entity_id == entity_id,
                JobRun.payload_json["source"].astext == source,
            )
            .with_only_columns(JobRun.id)
            .limit(1)
            .with_for_update()
        )
        return (await self.db.execute(stmt)).scalar_one_or_none() is not None

    async def lock_brand_upload(self) -> None:
        """The tenant's brand-upload advisory lock (transaction scope; like S13 Q10)."""
        self._own_session()
        await self.db.execute(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtextextended(f"brand_upload:{self.tenant_id}", 0)
                )
            )
        )

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
        zero_reservation: bool = False,
    ) -> JobRun | None:
        """The terminal write of the claimed attempt; None when the fence drops it. A terminal
        row is immutable, so the budget reservation is zeroed here or never (S18 K2, CP10)."""
        if status not in TERMINAL:
            raise ValueError(f"{status!r} is not a terminal status")
        values: dict[str, Any] = {
            "status": status,
            # SQL NULL, not JSON `null`: the JSONB columns would store 'null' for None
            "result_json": null() if result_json is None else result_json,
            "error_report_json": null() if error_report_json is None else error_report_json,
            "error_code": error_code,
            "lease_expires_at": None,
            "finished_at": func.now(),
        }
        if zero_reservation:
            values["cost_reserved_usd"] = 0
        return await self._update_returning(job_id, self._fence(job_id, attempt), values)

    async def lock_running(self, job_id: uuid.UUID, attempt: int) -> JobRun | None:
        """The claimed attempt's row `FOR UPDATE`, or None when the fence no longer holds (the
        lease was reaped and a newer attempt owns the row): S18's terminal persist runs only
        after this check."""
        stmt = (
            self._select()
            .where(*self._fence(job_id, attempt))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def reserve_cost(self, job_id: uuid.UUID, attempt: int, amount: Any) -> bool:
        """Set the claimed attempt's budget reservation (S18 D10); False when fenced out."""
        row = await self._update_returning(
            job_id, self._fence(job_id, attempt), {"cost_reserved_usd": amount}
        )
        return row is not None

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

    async def set_node(self, job_id: uuid.UUID, node: str, *, attempt: int | None = None) -> bool:
        """The transient pipeline stage of a running job (`emit_node_event`, BA:1536; S14b T5).
        Only a `running` row moves; a queued, terminal or foreign row is left as it is (False).
        With `attempt` (the pipeline's), it is fenced on that attempt too, so a reaped attempt
        never overwrites the node of the attempt that replaced it (KNOWN-ISSUES §E, S14b)."""
        where = (
            self._fence(job_id, attempt)
            if attempt is not None
            else self._where(job_id, JobRun.status == "running")
        )
        row = await self._update_returning(job_id, where, {"node": node})
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
                self.db,
                kind=done.kind,
                queue=done.queue,
                job_run_id=done.id,
                lock=lock_for(done.kind),
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

    async def orphaned_queued(self, *, limit: int = 100) -> list[uuid.UUID]:
        """This tenant's `queued` rows whose queue entry is orphaned (a worker killed between
        `release_retry`/`release_shutdown` and the queue's own re-queue), oldest first."""
        stmt = (
            self._select()
            .where(JobRun.status == "queued", JobRun.queue_job_id.is_not(None))
            .order_by(JobRun.queued_at)
            .limit(limit)
        )
        queued = {
            int(job.queue_job_id): job.id
            for job in (await self.db.execute(stmt)).scalars().all()
            if job.queue_job_id is not None and job.queue_job_id.isdigit()
        }
        if not queued:
            return []
        found = await self.db.execute(_ORPHAN_ENTRIES, {"ids": list(queued)})
        return [queued[row[0]] for row in found.all()]

    async def redefer_orphan(self, job_id: uuid.UUID) -> JobRun | None:
        """A `queued` row whose queue entry is orphaned gets a new entry (the old one retired,
        so it can never run); the attempt is unchanged. Locked `FOR UPDATE SKIP LOCKED` and
        re-checked under the lock, so a concurrent reaper never doubles it. None when the row
        no longer qualifies."""
        self._own_session()
        locked = await self.db.execute(
            self._select()
            .where(JobRun.id == job_id, JobRun.status == "queued")
            .with_for_update(skip_locked=True)
        )
        job = locked.scalar_one_or_none()
        if job is None or job.queue_job_id is None or not job.queue_job_id.isdigit():
            return None
        entry = await self.db.execute(_ORPHAN_ENTRIES, {"ids": [int(job.queue_job_id)]})
        if entry.first() is None:
            return None
        await retire_on_session(self.db, job.queue_job_id)
        queue_job_id = await defer_on_session(
            self.db, kind=job.kind, queue=job.queue, job_run_id=job.id, lock=lock_for(job.kind)
        )
        await self.db.execute(
            self._scoped_write(
                update(JobRun).where(*self._where(job_id)).values(queue_job_id=queue_job_id)
            )
        )
        set_committed_value(job, "queue_job_id", queue_job_id)
        return job

    # ---- the staff requeue (A15; DATABASE.md:312; BACKEND-ARCHITECTURE:990-1031) ----

    async def requeue(self, old_id: uuid.UUID) -> tuple[JobRun, JobRun]:
        """A NEW row for a `dead_letter` or `failed` job (CANON.md:61): the same kind, entity,
        listing, payload, `created_by` and `max_attempts`, `parent_job_id` = the old row, a fresh
        `correlation_id`, no idempotency key, and a new queue entry. The old row is untouched
        (it is terminal). Returns (old, new); `NotEligibleError` otherwise."""
        self._own_session()
        old = (
            await self.db.execute(self._select().where(JobRun.id == old_id).with_for_update())
        ).scalar_one_or_none()
        if old is None:
            raise NotEligibleError("unknown job")
        if old.status not in ("dead_letter", "failed"):
            raise NotEligibleError("only a dead_letter or failed job is requeued")
        tenant_ok = (
            await self.db.execute(
                self._select()
                .where(JobRun.id == old_id, _tenant_running())
                .with_only_columns(JobRun.id)
            )
        ).scalar_one_or_none()
        if tenant_ok is None:
            raise NotEligibleError("the tenant is not trial or active")
        child = (
            await self.db.execute(
                self._select().where(JobRun.parent_job_id == old_id).with_only_columns(JobRun.id)
            )
        ).scalar_one_or_none()
        if child is not None:
            raise NotEligibleError("the job was already requeued")
        new = await self.enqueue(
            EnqueueSpec(
                kind=JobKind(old.kind),
                entity_type=old.entity_type,
                entity_id=old.entity_id,
                created_by=old.created_by,
                listing_id=old.listing_id,
                payload=dict(old.payload_json or {}),
                batch_id=old.batch_id,
                parent_job_id=old.id,
                max_attempts=old.max_attempts,
            )
        )
        return old, new.job
