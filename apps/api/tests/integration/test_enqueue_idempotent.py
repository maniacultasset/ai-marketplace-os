"""S13 A3 (AC2; DATABASE.md:940): `job_runs.idempotency_key` dedupes an enqueue, concurrent
ones included, per tenant; a NULL key never dedupes; the payload cap and the tenant guard."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from app.db.guard import CrossTenantQueryError
from app.db.session import unit_of_work
from app.repositories.jobs import Enqueued, EnqueueSpec, JobRunRepository, job_idempotency_key
from mkos_schemas.enums import JobKind

from .conftest import LOCAL_HOSTS
from .jobs_support import JobWorld, count, jobs_db

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["jobs_db"]


def _spec(jw: JobWorld, slug: str = "acme", **extra: object) -> EnqueueSpec:
    return EnqueueSpec(
        kind=JobKind.BULK_IMPORT,
        entity_type="import",
        entity_id=uuid.uuid4(),
        created_by=jw.owner(slug),
        **extra,  # type: ignore[arg-type]
    )


async def _enqueue(jw: JobWorld, spec: EnqueueSpec, slug: str = "acme") -> Enqueued:
    tenant = jw.tenant(slug)
    async with unit_of_work(tenant) as session:
        return await JobRunRepository(session, tenant).enqueue(spec)


def _totals(jw: JobWorld) -> tuple[int, int]:
    return (
        count(jw.conn, "SELECT count(*) FROM job_runs"),
        count(jw.conn, "SELECT count(*) FROM procrastinate_jobs"),
    )


async def test_enqueue_idempotent_same_key_twice(jobs_db: JobWorld) -> None:
    first = await _enqueue(jobs_db, _spec(jobs_db, http_key="key-1"))
    second = await _enqueue(jobs_db, _spec(jobs_db, http_key="key-1"))
    assert (first.created, second.created) == (True, False)
    assert first.job.id == second.job.id
    assert second.job.queue_job_id == first.job.queue_job_id
    assert _totals(jobs_db) == (1, 1)


async def test_enqueue_idempotent_5_concurrent(jobs_db: JobWorld) -> None:
    results = await asyncio.gather(
        *(_enqueue(jobs_db, _spec(jobs_db, http_key="key-5")) for _ in range(5))
    )
    assert sum(r.created for r in results) == 1
    assert len({r.job.id for r in results}) == 1
    assert _totals(jobs_db) == (1, 1)


async def test_enqueue_idempotent_null_key_twice_two_jobs(jobs_db: JobWorld) -> None:
    first = await _enqueue(jobs_db, _spec(jobs_db))
    second = await _enqueue(jobs_db, _spec(jobs_db))
    assert first.created and second.created
    assert first.job.id != second.job.id
    assert first.job.idempotency_key is None
    assert _totals(jobs_db) == (2, 2)


async def test_enqueue_idempotent_same_key_other_tenant_independent(jobs_db: JobWorld) -> None:
    a = await _enqueue(jobs_db, _spec(jobs_db, http_key="shared"))
    b = await _enqueue(jobs_db, _spec(jobs_db, "bharat", http_key="shared"), "bharat")
    assert a.created and b.created
    assert a.job.tenant_id != b.job.tenant_id
    assert _totals(jobs_db) == (2, 2)


def test_job_idempotency_key_vector() -> None:
    assert (
        job_idempotency_key("key-1", "bulk_import", None)
        == "aea47b1a323886fc1a8a45b14b2bf213ae1eebdd69e6ae5ef6be6f26caeeb88a"
    )
    listing = uuid.UUID("00000000-0000-0000-0000-000000000001")
    assert (
        job_idempotency_key("key-1", "listing_generate", listing)
        == "8d498198ed26f57d0d04c053bd3cc67d9f036b4126c426303c3f6b9ffa34b0b8"
    )


async def test_enqueue_payload_over_64k_refused(jobs_db: JobWorld) -> None:
    big = {"ids": ["x" * 1000 for _ in range(70)]}
    with pytest.raises(ValueError, match="payload exceeds 65536 bytes"):
        await _enqueue(jobs_db, _spec(jobs_db, payload=big))
    assert _totals(jobs_db) == (0, 0)


async def test_enqueue_other_tenant_refused(jobs_db: JobWorld) -> None:
    acme, bharat = jobs_db.tenant("acme"), jobs_db.tenant("bharat")
    with pytest.raises(CrossTenantQueryError):
        async with unit_of_work(acme) as session:
            await JobRunRepository(session, bharat).enqueue(_spec(jobs_db, "bharat"))
    assert _totals(jobs_db) == (0, 0)


async def test_enqueue_sets_max_attempts_from_policy(jobs_db: JobWorld) -> None:
    done = await _enqueue(jobs_db, _spec(jobs_db))
    assert done.job.max_attempts == 3
    assert done.job.attempt == 0 and done.job.status == "queued"
