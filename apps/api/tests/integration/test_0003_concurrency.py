"""Concurrency attacks on revisions 0003/0003b by the step's tester, with committed writes on a
dedicated head database (never `head_db`): the idempotency protocol's concurrent duplicate
(waits, then replays or 409s by hash; another tenant's same key never waits), an OAuth state
consumed once under a race, two jobs racing for one open submission, a duplicate job delivery
racing the claim, and procrastinate 3.10 itself driving a worker as `app_rw` (defer, fetch, run,
retry to failure, cancel, periodic defer, heartbeat, prune, delete old jobs)."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any
from uuid import UUID

import psycopg
import pytest

from .conftest import ScratchDb, head_database
from .ops_support import TenantSeed, digest, job_run, oauth_state, seed_tenant, submission

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]

IDEMPOTENT_INSERT = (
    "INSERT INTO idempotency_keys (tenant_id, key, endpoint, request_hash, user_id, expires_at) "
    "VALUES (%s, %s, 'POST /api/v1/listings/generate', %s, %s, now() + interval '24 hours') "
    "ON CONFLICT DO NOTHING RETURNING key"
)
CONSUME_STATE = (
    "DELETE FROM oauth_states WHERE state_hash = %s AND expires_at > now() RETURNING tenant_id"
)
CLAIM = (
    "UPDATE job_runs SET status = 'running', attempt = attempt + 1, started_at = now(), "
    "lease_expires_at = now() + interval '180 seconds' "
    "WHERE id = %s AND status = 'queued' AND attempt < max_attempts RETURNING attempt"
)


@pytest.fixture(scope="module")
def ops_db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ScratchDb]:
    with head_database(tmp_path_factory.mktemp("ops_concurrency")) as db:
        yield db


def _connect(db: ScratchDb, *, autocommit: bool = False) -> Conn:
    conn = psycopg.connect(db.libpq, autocommit=autocommit, connect_timeout=5)
    conn.execute("SET lock_timeout = '10s'")
    conn.commit()
    return conn


@pytest.fixture
def seeded(ops_db: ScratchDb) -> Iterator[tuple[TenantSeed, TenantSeed]]:
    """Two committed tenants, deleted (one cascade each) afterwards."""
    with _connect(ops_db) as conn:
        a, b = seed_tenant(conn), seed_tenant(conn)
        conn.commit()
    yield a, b
    with _connect(ops_db, autocommit=True) as conn:
        conn.execute("DELETE FROM tenants WHERE id IN (%s, %s)", (a.tenant_id, b.tenant_id))


def _wait_until_blocked(db: ScratchDb, pid: int) -> None:
    with _connect(db, autocommit=True) as probe:
        for _ in range(100):
            row = probe.execute(
                "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", (pid,)
            ).fetchone()
            if row is not None and row[0] == "Lock":
                return
            time.sleep(0.05)
    raise AssertionError(f"backend {pid} never waited on a lock")


def _race(
    db: ScratchDb, first: Callable[[Conn], Any], second: Callable[[Conn], Any]
) -> tuple[Any, Any]:
    """Run `first` in an open transaction, start `second` on another connection, prove it waits
    on a lock, commit `first`, and return both results (or the second's exception)."""
    with _connect(db) as one, _connect(db) as two, ThreadPoolExecutor(max_workers=1) as pool:
        result_one = first(one)
        pid = two.info.backend_pid

        def run_second() -> Any:
            try:
                value = second(two)
                two.commit()
                return value
            except psycopg.Error as error:
                two.rollback()
                return error

        pending: Future[Any] = pool.submit(run_second)
        _wait_until_blocked(db, pid)
        assert not pending.done()
        one.commit()
        return result_one, pending.result(timeout=15)


def test_idempotency_concurrent_duplicate_waits_then_replays(
    ops_db: ScratchDb, seeded: tuple[TenantSeed, TenantSeed]
) -> None:
    """DATABASE.md §5.4 protocol: a concurrent duplicate waits, then sees no insert and reads
    the stored row (same hash → replay; a different hash → 409); the PK is per tenant, so the
    other tenant's identical key neither waits nor collides."""
    a, b = seeded
    key = "client-key-1"

    def first(conn: Conn) -> Any:
        return conn.execute(
            IDEMPOTENT_INSERT, (a.tenant_id, key, digest("body"), a.user_id)
        ).fetchall()

    def second(conn: Conn) -> Any:
        inserted = conn.execute(
            IDEMPOTENT_INSERT, (a.tenant_id, key, digest("other body"), a.user_id)
        ).fetchall()
        stored = conn.execute(
            "SELECT request_hash FROM idempotency_keys WHERE tenant_id = %s AND key = %s",
            (a.tenant_id, key),
        ).fetchone()
        return inserted, stored

    won, (lost, stored) = _race(ops_db, first, second)
    assert won == [(key,)]
    assert lost == []
    assert stored == (digest("body"),)  # the waiter compares hashes: different body → 409
    with _connect(ops_db) as other:
        assert other.execute(
            IDEMPOTENT_INSERT, (b.tenant_id, key, digest("body"), b.user_id)
        ).fetchall() == [(key,)]
        other.commit()


def test_oauth_state_consumed_once_under_race(
    ops_db: ScratchDb, seeded: tuple[TenantSeed, TenantSeed]
) -> None:
    a, _ = seeded
    state = digest("race-state")
    with _connect(ops_db) as conn:
        oauth_state(conn, a, state_hash=state)
        conn.commit()

    def consume(conn: Conn) -> Any:
        return conn.execute(CONSUME_STATE, (state,)).fetchall()

    one, two = _race(ops_db, consume, consume)
    assert one == [(a.tenant_id,)]
    assert two == []


def test_two_jobs_race_for_one_open_submission(
    ops_db: ScratchDb, seeded: tuple[TenantSeed, TenantSeed]
) -> None:
    """`ux_ls_open`: two publish jobs writing a `pending` row for one listing at once (different
    versions, so only the open-submission index stands between them): the second waits, then
    fails 23505 on `ux_ls_open` once the first commits."""
    a, _ = seeded
    with _connect(ops_db) as conn:
        from . import factories as f

        v2 = f.version(conn, a.tenant_id, a.listing_id, generated_by_user_id=a.user_id)
        conn.commit()

    def first(conn: Conn) -> UUID:
        return submission(conn, a)

    def second(conn: Conn) -> UUID:
        return submission(conn, a, listing_version_id=v2)

    # 0004's ls_guard locks the listing and refuses an unapproved version first
    # (test_0004_ls_guard.py races it); this 0003 race isolates the index on this private db.
    with _connect(ops_db, autocommit=True) as admin:
        admin.execute("ALTER TABLE listing_submissions DISABLE TRIGGER ls_guard")
    try:
        _, error = _race(ops_db, first, second)
    finally:
        with _connect(ops_db, autocommit=True) as admin:
            admin.execute("ALTER TABLE listing_submissions ENABLE TRIGGER ls_guard")
    assert isinstance(error, psycopg.errors.UniqueViolation)
    assert error.diag.constraint_name == "ux_ls_open"


def test_duplicate_delivery_races_the_claim(
    ops_db: ScratchDb, seeded: tuple[TenantSeed, TenantSeed]
) -> None:
    """Two workers receive the same job: the claim compare-and-set lets exactly one run it."""
    a, _ = seeded
    with _connect(ops_db) as conn:
        job = job_run(conn, a.tenant_id)
        conn.commit()

    def claim(conn: Conn) -> Any:
        return conn.execute(CLAIM, (job,)).fetchall()

    one, two = _race(ops_db, claim, claim)
    assert one == [(1,)]
    assert two == []


async def test_procrastinate_worker_lifecycle_as_app_rw(ops_db: ScratchDb) -> None:
    """procrastinate 3.10 drives every query of a worker's life against the 0003b schema as the
    runtime role: no permission error anywhere (the GRANT loop, the default table and sequence
    privileges), and the job states land as the library expects."""
    from procrastinate import App, PsycopgConnector, RetryStrategy

    connector = PsycopgConnector(conninfo=ops_db.libpq, kwargs={"options": "-c role=app_rw"})
    app = App(connector=connector)
    ran: list[str] = []

    @app.task(name="s8_probe", queue="default")
    async def probe(job_run_id: str) -> None:
        ran.append(job_run_id)

    @app.task(name="s8_boom", queue="publish", retry=RetryStrategy(max_attempts=1, wait=0))
    async def boom(job_run_id: str) -> None:
        raise RuntimeError(job_run_id)

    @app.task(name="s8_periodic", queue="default")
    async def periodic(timestamp: int) -> None:
        del timestamp

    async with app.open_async():
        who = await connector.execute_query_one_async("SELECT current_user AS who")
        assert who["who"] == "app_rw"
        await probe.defer_async(job_run_id="ok")
        await boom.defer_async(job_run_id="fails")
        await app.run_worker_async(
            queues=["default", "publish"], wait=False, install_signal_handlers=False
        )
        assert ran == ["ok"]
        manager = app.job_manager
        statuses = sorted(
            (job.task_name, job.status, job.attempts) for job in await manager.list_jobs_async()
        )
        assert statuses == [("s8_boom", "failed", 2), ("s8_probe", "succeeded", 1)]
        later = await probe.configure(schedule_in={"hours": 1}).defer_async(job_run_id="later")
        assert await manager.cancel_job_by_id_async(later) is True
        tick = periodic.configure(task_kwargs={"timestamp": 1}).job
        first_tick = await manager.defer_periodic_job(job=tick, periodic_id="s8", defer_timestamp=1)
        assert first_tick is not None
        # procrastinate_periodic_defers dedupes the same tick across workers
        assert (
            await manager.defer_periodic_job(job=tick, periodic_id="s8", defer_timestamp=1) is None
        )
        assert await manager.cancel_job_by_id_async(first_tick) is True
        assert await manager.list_queues_async()
        assert await manager.list_tasks_async()
        assert await manager.list_locks_async() == []
        worker_id = await manager.register_worker()
        await manager.update_heartbeat(worker_id)
        assert await manager.get_stalled_jobs(seconds_since_heartbeat=1) == []
        await manager.prune_stalled_workers(seconds_since_heartbeat=0)
        await manager.unregister_worker(worker_id)
        await manager.delete_old_jobs(nb_hours=0, include_failed=True, include_cancelled=True)
        remaining = await connector.execute_query_all_async(
            "SELECT id, status::text AS status FROM procrastinate_jobs"
        )
        # the job scheduled an hour ahead keeps a future `scheduled` event, so it is not old yet
        assert remaining == [{"id": later, "status": "cancelled"}]
