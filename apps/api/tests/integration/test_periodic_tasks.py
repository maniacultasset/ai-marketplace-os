"""S13 A10 (AC11; PLAN D12): the periodic prunes and `expire_trials`, each slot firing once across
workers, and periodic queue entries carrying only `{"timestamp"}`."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.db.queue import new_queue_app
from app.workers.periodic import (
    PERIODIC,
    prune_idempotency_keys,
    prune_rate_limits,
    register_periodic,
    run_expire_trials,
)
from app.workers.tasks import register_tasks
from procrastinate.periodic import PeriodicDeferrer

from .conftest import LOCAL_HOSTS
from .jobs_support import JobWorld, count, jobs_db

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["jobs_db"]


def test_periodic_table_literal() -> None:
    assert {k: v[0] for k, v in PERIODIC.items()} == {
        "reaper": "* * * * *",
        "prune_rate_limits": "*/5 * * * *",
        "prune_idempotency_keys": "0 * * * *",
        "expire_trials": "15 * * * *",
        "retention_daily": "0 3 * * *",
        "marketplace_token_refresh": "*/5 * * * *",  # S15
        "oauth_states_prune": "30 * * * *",  # S15
        "schedule_status_sync": "* * * * *",  # S21
        "check_alerts": "*/5 * * * *",  # S23
    }


async def test_periodic_slot_fires_once_across_two_workers(jobs_db: JobWorld) -> None:
    fired: list[int] = []

    def build() -> Any:
        app = new_queue_app()

        @app.periodic(cron="* * * * * *", periodic_id="probe", queueing_lock="probe")
        @app.task(name="probe", queue="default")
        async def probe(timestamp: int) -> None:
            fired.append(timestamp)

        return app

    apps = [build(), build()]

    async def work(app: Any) -> None:
        async with app.open_async():
            await app.run_worker_async(
                name="probe",
                wait=True,
                fetch_job_polling_interval=0.2,
                install_signal_handlers=False,
                listen_notify=True,
            )

    tasks = [asyncio.create_task(work(a)) for a in apps]
    await asyncio.sleep(4.5)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert len(fired) >= 2
    assert len(fired) == len(set(fired))  # one run per slot, never two
    slots = count(jobs_db.conn, "SELECT count(*) FROM procrastinate_periodic_defers")
    assert slots >= 1


def _add_key(jw: JobWorld, slug: str, key: str, expires: datetime) -> None:
    jw.conn.execute(
        "INSERT INTO idempotency_keys (tenant_id, key, endpoint, request_hash, user_id, "
        "expires_at) VALUES (%s, %s, 'POST /x', %s, %s, %s)",
        [jw.tenant(slug), key, b"\x00" * 32, jw.owner(slug), expires],
    )


async def test_prune_idempotency_expired_only(jobs_db: JobWorld) -> None:
    past, future = datetime.now(UTC) - timedelta(minutes=1), datetime.now(UTC) + timedelta(hours=1)
    for slug in ("acme", "bharat"):
        _add_key(jobs_db, slug, "old", past)
        _add_key(jobs_db, slug, "live", future)
    assert await prune_idempotency_keys() == 2
    rows = jobs_db.conn.execute(
        "SELECT tenant_id, key FROM idempotency_keys ORDER BY tenant_id, key"
    ).fetchall()
    assert sorted(rows) == sorted(
        [(jobs_db.tenant("acme"), "live"), (jobs_db.tenant("bharat"), "live")]
    )


async def test_prune_rate_limits_older_than_5m(jobs_db: JobWorld) -> None:
    now = datetime.now(UTC)
    for minutes in (0, 4, 6, 60):
        jobs_db.conn.execute(
            "INSERT INTO rate_limit_counters (bucket_key, window_start, n) VALUES (%s, %s, 1)",
            [f"b{minutes}", now - timedelta(minutes=minutes)],
        )
    assert await prune_rate_limits(now) == 2
    keys = {r[0] for r in jobs_db.conn.execute("SELECT bucket_key FROM rate_limit_counters")}
    assert keys == {"b0", "b4"}


async def test_expire_trials_runs(jobs_db: JobWorld) -> None:
    from .auth_support import add_tenant

    over = add_tenant(jobs_db.conn, "trial-over", status="trial")
    live = add_tenant(jobs_db.conn, "trial-live", status="trial")
    jobs_db.conn.execute(
        "UPDATE tenants SET trial_ends_at = now() - interval '1 hour' WHERE id = %s", [over]
    )
    assert await run_expire_trials() == 1
    status: dict[Any, Any] = dict(jobs_db.conn.execute("SELECT id, status FROM tenants").fetchall())
    assert (status[over], status[live]) == ("suspended", "trial")


async def test_periodic_args_only_timestamp(jobs_db: JobWorld) -> None:
    app = new_queue_app()
    register_tasks(app)
    register_periodic(app)
    now = int(time.time())
    async with app.open_async():
        deferrer = PeriodicDeferrer(registry=app.periodic_registry)
        await deferrer.defer_jobs([(p, now) for p in app.periodic_registry.periodic_tasks.values()])
    rows = jobs_db.conn.execute(
        "SELECT task_name, args, queueing_lock FROM procrastinate_jobs ORDER BY task_name"
    ).fetchall()
    assert [r[0] for r in rows] == sorted(f"periodic_{k}" for k in PERIODIC)
    for _name, args, lock in rows:
        assert args == {"timestamp": now}
        assert lock in PERIODIC
