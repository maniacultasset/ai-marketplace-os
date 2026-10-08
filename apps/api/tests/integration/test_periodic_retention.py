"""S13 A11 (AC11; DATABASE.md:1836-1846): the daily retention pass, as app_rw, tenant by tenant:
children first, whole requeue chains only, failed jobs 180 days, publish jobs kept, batches of
1,000, and the queue's finished entries (succeeded 7 days, others 90)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.db.queue import new_queue_app
from app.db.session import unit_of_work
from app.repositories import retention
from app.workers.periodic import retention_daily
from psycopg import sql

from .conftest import LOCAL_HOSTS
from .jobs_support import JobWorld, count, insert_job, jobs_db

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["jobs_db"]


def _ago(days: float) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


def _old_job(
    jw: JobWorld, slug: str, *, days: float, status: str = "succeeded", **extra: Any
) -> uuid.UUID:
    columns: dict[str, Any] = {
        "status": status,
        "attempt": 1,
        "queued_at": _ago(days + 1),
        "started_at": _ago(days + 1),
        "finished_at": _ago(days),
        **extra,
    }
    if status in ("failed", "dead_letter"):
        columns["error_report_json"] = {"status": "failed"}
    if status in ("queued",):
        columns.update(finished_at=None, started_at=None, attempt=0)
    return insert_job(jw.conn, jw.tenant(slug), **columns)


def _import(jw: JobWorld, slug: str, job: uuid.UUID, *, days: float, rows: int = 2) -> uuid.UUID:
    tenant = jw.tenant(slug)
    row = jw.conn.execute(
        "INSERT INTO imports (tenant_id, storage_key, filename, status, imported_rows, failed_rows, "
        "job_id, created_by, created_at) VALUES (%s, %s, 'c.csv', 'succeeded', %s, 0, %s, %s, %s) "
        "RETURNING id",
        [tenant, f"{tenant}/imports/{uuid.uuid4()}.csv", rows, job, jw.owner(slug), _ago(days)],
    ).fetchone()
    assert row is not None
    for n in range(rows):
        jw.conn.execute(
            "INSERT INTO import_rows (tenant_id, import_id, row_number, raw_row, status, job_id) "
            "VALUES (%s, %s, %s, '{}', 'imported', %s)",
            [tenant, row[0], n + 2, job],
        )
    return row[0]  # type: ignore[no-any-return]


def _exists(jw: JobWorld, table: str, row_id: uuid.UUID) -> bool:
    query = sql.SQL("SELECT count(*) FROM {} WHERE id = %s").format(sql.Identifier(table))
    row = jw.conn.execute(query, [row_id]).fetchone()
    return row is not None and row[0] == 1


async def test_retention_children_first(jobs_db: JobWorld) -> None:
    job = _old_job(jobs_db, "acme", days=100)
    imp = _import(jobs_db, "acme", job, days=100)
    totals = await retention_daily()
    assert totals == {"import_rows": 2, "imports": 1, "job_runs": 1, "image_orphans": 0}
    assert not _exists(jobs_db, "imports", imp) and not _exists(jobs_db, "job_runs", job)


async def test_retention_keeps_young_import_and_its_job(jobs_db: JobWorld) -> None:
    job = _old_job(jobs_db, "acme", days=100)
    imp = _import(jobs_db, "acme", job, days=10)
    await retention_daily()
    assert _exists(jobs_db, "imports", imp) and _exists(jobs_db, "job_runs", job)
    assert count(jobs_db.conn, "SELECT count(*) FROM import_rows") == 2


async def test_retention_chain_all_past_cutoff(jobs_db: JobWorld) -> None:
    root = _old_job(jobs_db, "acme", days=200, status="dead_letter")
    child = _old_job(jobs_db, "acme", days=100, parent_job_id=root)
    await retention_daily()
    assert not _exists(jobs_db, "job_runs", root) and not _exists(jobs_db, "job_runs", child)


async def test_retention_keeps_chain_with_live_member(jobs_db: JobWorld) -> None:
    root = _old_job(jobs_db, "acme", days=200, status="dead_letter")
    child = _old_job(jobs_db, "acme", days=0, status="queued", parent_job_id=root)
    young = _old_job(jobs_db, "acme", days=300, status="dead_letter")
    _old_job(jobs_db, "acme", days=10, parent_job_id=young)
    await retention_daily()
    for kept in (root, child, young):
        assert _exists(jobs_db, "job_runs", kept)


async def test_retention_failed_kept_180_days(jobs_db: JobWorld) -> None:
    failed_100 = _old_job(jobs_db, "acme", days=100, status="failed")
    dead_100 = _old_job(jobs_db, "acme", days=100, status="dead_letter")
    ok_100 = _old_job(jobs_db, "acme", days=100)
    ok_80 = _old_job(jobs_db, "acme", days=80)
    failed_200 = _old_job(jobs_db, "acme", days=200, status="failed")
    await retention_daily()
    assert [
        _exists(jobs_db, "job_runs", j) for j in (failed_100, dead_100, ok_100, ok_80, failed_200)
    ] == [
        True,
        True,
        False,
        True,
        False,
    ]


async def test_retention_keeps_publish_jobs(jobs_db: JobWorld) -> None:
    # the publish gate and the listing FKs are not under test here: insert as a harness reset
    jobs_db.conn.execute("SET session_replication_role = replica")
    try:
        job = _old_job(
            jobs_db,
            "acme",
            days=400,
            kind="listing_publish",
            queue="publish",
            entity_type="listing",
            listing_id=uuid.uuid4(),
            created_by=jobs_db.owner("acme"),
        )
    finally:
        jobs_db.conn.execute("RESET session_replication_role")
    await retention_daily()
    assert _exists(jobs_db, "job_runs", job)


async def test_retention_batches_of_1000(jobs_db: JobWorld) -> None:
    tenant = jobs_db.tenant("acme")
    jobs_db.conn.execute(
        "INSERT INTO job_runs (tenant_id, kind, entity_type, entity_id, payload_json, "
        "correlation_id, status, attempt, queued_at, started_at, finished_at) "
        "SELECT %s, 'bulk_import', 'import', gen_random_uuid(), '{}', gen_random_uuid(), "
        "'succeeded', 1, now() - interval '101 days', now() - interval '101 days', "
        "now() - interval '100 days' FROM generate_series(1, 2500)",
        [tenant],
    )
    async with unit_of_work(tenant) as session:
        counts = await retention.prune_tenant(session, tenant)
    assert counts["job_runs"] == (2500, 3)
    assert count(jobs_db.conn, "SELECT count(*) FROM job_runs") == 0


async def test_retention_other_tenant_untouched(jobs_db: JobWorld) -> None:
    mine = _old_job(jobs_db, "acme", days=100)
    theirs = _old_job(jobs_db, "bharat", days=100)
    acme = jobs_db.tenant("acme")
    async with unit_of_work(acme) as session:
        await retention.prune_tenant(session, acme)
    assert not _exists(jobs_db, "job_runs", mine)
    assert _exists(jobs_db, "job_runs", theirs)


def _queue_entry(jw: JobWorld, status: str, days: float) -> int:
    row = jw.conn.execute(
        "INSERT INTO procrastinate_jobs (queue_name, task_name, args) "
        "VALUES ('default', 'bulk_import', '{}') RETURNING id"
    ).fetchone()
    assert row is not None
    # todo -> doing -> final, so the status triggers write the events delete_old_jobs reads
    jw.conn.execute("UPDATE procrastinate_jobs SET status = 'doing' WHERE id = %s", [row[0]])
    jw.conn.execute("UPDATE procrastinate_jobs SET status = %s WHERE id = %s", [status, row[0]])
    jw.conn.execute(
        "UPDATE procrastinate_events SET at = %s WHERE job_id = %s", [_ago(days), row[0]]
    )
    return int(row[0])


async def test_retention_queue_succeeded_7_days(jobs_db: JobWorld) -> None:
    old_ok = _queue_entry(jobs_db, "succeeded", 8)
    new_ok = _queue_entry(jobs_db, "succeeded", 6)
    old_failed = _queue_entry(jobs_db, "failed", 8)
    ancient_failed = _queue_entry(jobs_db, "failed", 91)
    app = new_queue_app()
    async with app.open_async():
        await retention_daily(app.job_manager)
    left = {r[0] for r in jobs_db.conn.execute("SELECT id FROM procrastinate_jobs")}
    assert old_ok not in left and ancient_failed not in left
    assert {new_ok, old_failed} <= left


async def test_retention_chain_split_across_batches(
    jobs_db: JobWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a batch boundary inside a chain must never leave a child pointing at a deleted parent
    monkeypatch.setattr(retention, "BATCH", 1)
    root = _old_job(jobs_db, "acme", days=300, status="dead_letter")
    child = _old_job(jobs_db, "acme", days=250, status="dead_letter", parent_job_id=root)
    grandchild = _old_job(jobs_db, "acme", days=200, status="dead_letter", parent_job_id=child)
    acme = jobs_db.tenant("acme")
    async with unit_of_work(acme) as session:
        counts = await retention.prune_tenant(session, acme)
    assert counts["job_runs"] == (3, 4)
    assert not any(_exists(jobs_db, "job_runs", j) for j in (root, child, grandchild))
