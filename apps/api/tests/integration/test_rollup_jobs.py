"""S23 T10 (AC6; feeds AC11; R12661a): the job and queue rollups, cross-tenant through
`platform_scope()`."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg
import pytest

from .conftest import LOCAL_HOSTS
from .ops_support import job_run

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

Conn = psycopg.Connection[tuple[Any, ...]]
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def job(
    conn: Conn,
    tenant: UUID,
    status: str,
    *,
    kind: str = "bulk_import",
    queue: str = "default",
    queued: datetime | None = None,
    started: datetime | None = None,
    finished: datetime | None = None,
) -> UUID:
    """A job row placed directly (the queue trigger off, so `queue` is the given one)."""
    columns: dict[str, Any] = {"status": status, "kind": kind, "queue": queue}
    if kind == "brand_embed":
        columns.update(entity_type="brand_document", payload_json={})
    if queued is not None:
        columns["queued_at"] = queued
    if started is not None:
        columns["started_at"] = started
    if status == "running":
        columns["lease_expires_at"] = NOW + timedelta(hours=1)
        columns.setdefault("started_at", NOW - timedelta(minutes=1))
    if finished is not None:
        columns["finished_at"] = finished
    if status in ("failed", "dead_letter"):
        columns["error_report_json"] = {"errors": []}
    conn.execute("SET session_replication_role = replica")
    try:
        return job_run(conn, tenant, **columns)
    finally:
        conn.execute("RESET session_replication_role")


async def _ro[T](fn: Callable[..., Awaitable[T]], *args: Any) -> T:
    from app.workers.rollup import rollup_session

    async with rollup_session() as session:
        return await fn(session, *args)


@pytest.fixture
def tenants(products_world: Any) -> tuple[Conn, UUID, UUID]:
    return products_world.conn, products_world.tenant("acme"), products_world.tenant("bharat")


async def test_job_outcomes_window_edges(tenants: tuple[Conn, UUID, UUID]) -> None:
    from app.workers.rollup import KindOutcomes, job_outcomes

    conn, acme, _ = tenants
    started = NOW - timedelta(minutes=30)
    job(conn, acme, "failed", started=started, finished=NOW - timedelta(minutes=14, seconds=59))
    job(conn, acme, "succeeded", started=started, finished=NOW - timedelta(minutes=15, seconds=1))
    job(conn, acme, "succeeded", started=started, finished=NOW)
    job(conn, acme, "running")
    job(conn, acme, "queued")
    assert await _ro(job_outcomes, NOW) == [KindOutcomes("bulk_import", 2, 1)]


async def test_job_outcomes_counts_across_tenants(tenants: tuple[Conn, UUID, UUID]) -> None:
    from app.workers.rollup import KindOutcomes, job_outcomes

    conn, acme, bharat = tenants
    started = NOW - timedelta(minutes=5)
    for tenant, status in ((acme, "dead_letter"), (bharat, "failed"), (bharat, "partial")):
        job(conn, tenant, status, started=started, finished=NOW - timedelta(minutes=1))
    job(conn, acme, "cancelled", finished=NOW - timedelta(minutes=1))
    job(
        conn,
        bharat,
        "succeeded",
        kind="brand_embed",
        started=started,
        finished=NOW - timedelta(minutes=2),
    )
    assert await _ro(job_outcomes, NOW) == [
        KindOutcomes("brand_embed", 1, 0),
        KindOutcomes("bulk_import", 4, 2),
    ]


async def test_oldest_queued_per_queue(tenants: tuple[Conn, UUID, UUID]) -> None:
    from app.workers.rollup import QueueAge, oldest_queued

    conn, acme, bharat = tenants
    assert await _ro(oldest_queued, NOW) == [QueueAge("default", None), QueueAge("publish", None)]
    job(conn, acme, "queued", queued=NOW - timedelta(seconds=600))
    job(conn, bharat, "queued", queued=NOW - timedelta(seconds=30))
    job(conn, bharat, "running", queued=NOW - timedelta(hours=2))
    job(conn, acme, "queued", queue="publish", queued=NOW - timedelta(seconds=599))
    assert await _ro(oldest_queued, NOW) == [
        QueueAge("default", 600),
        QueueAge("publish", 599),
    ]


async def test_processing_time_excludes_unstarted(tenants: tuple[Conn, UUID, UUID]) -> None:
    from app.workers.rollup import KindTiming, processing_time

    conn, acme, _ = tenants
    end = NOW - timedelta(minutes=1)
    job(conn, acme, "succeeded", started=end - timedelta(seconds=10), finished=end)
    job(conn, acme, "failed", started=end - timedelta(seconds=30), finished=end)
    job(conn, acme, "cancelled", finished=end)  # never started
    assert await _ro(processing_time, NOW) == [KindTiming("bulk_import", 2, 20.0, 30.0)]


async def test_rollup_jobs_read_only(tenants: tuple[Conn, UUID, UUID]) -> None:
    from app.workers.rollup import rollup_session
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    async with rollup_session() as session:
        with pytest.raises(DBAPIError) as caught:
            await session.execute(text("UPDATE job_runs SET node = 'x'"))
    assert getattr(caught.value.orig, "sqlstate", None) == "25006"
