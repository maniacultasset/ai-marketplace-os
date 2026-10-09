"""S13 A8 (DW8, AC5; spike REPORT C7): the worker process on both queues; SIGTERM lets a short job
finish, releases a long one uncounted for the next worker, exits 0 within 10 s and unregisters.
Subprocess legs run `tests/support/run_worker.py`, the real `app.workers.main.run()`."""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from app.db.session import unit_of_work
from app.repositories.jobs import EnqueueSpec, JobRunRepository
from app.workers import main
from app.workers.registry import JobContext, Outcome
from mkos_schemas.enums import JobKind
from tests.timing import budget

from . import factories as f
from .conftest import LOCAL_HOSTS
from .jobs_support import JobWorld, count, eventually, job_row, jobs_db, override_body

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["jobs_db", "override_body"]
RUN_WORKER = Path(__file__).resolve().parents[1] / "support" / "run_worker.py"


async def _enqueue(
    jw: JobWorld, kind: JobKind = JobKind.BULK_IMPORT, *, sleep: float = 0
) -> uuid.UUID:
    tenant = jw.tenant("acme")
    listing = f.listing(jw.conn, tenant) if kind is JobKind.LISTING_STATUS_SYNC else None
    async with unit_of_work(tenant) as session:
        done = await JobRunRepository(session, tenant).enqueue(
            EnqueueSpec(
                kind=kind,
                entity_type="listing" if listing else "import",
                entity_id=listing or uuid.uuid4(),
                created_by=None,
                listing_id=listing,
                payload={"sleep": sleep},
            )
        )
    return done.job.id


def spawn_worker(tmp_path: Path, *args: str) -> subprocess.Popen[str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "APP_ENV": "local",
        "APP_ROLE": "worker",
        "DATABASE_URL": os.environ["DATABASE_URL"],  # app_rw (the auth_db fixture)
        "LOG_FORMAT": "json",
        "LOG_LEVEL": "INFO",
    }
    return subprocess.Popen(
        [sys.executable, str(RUN_WORKER), *args],
        env=env,
        cwd=tmp_path,  # no developer .env
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _status(jw: JobWorld, job: uuid.UUID) -> str:
    return str(job_row(jw.conn, job)["status"])


def _stop(proc: subprocess.Popen[str]) -> tuple[int, float, str]:
    started = time.monotonic()
    proc.send_signal(signal.SIGTERM)
    out, err = proc.communicate(timeout=30)
    return proc.returncode, time.monotonic() - started, out + err


async def test_worker_sigterm_short_job_finishes(jobs_db: JobWorld, tmp_path: Path) -> None:
    job = await _enqueue(jobs_db, sleep=2)
    proc = spawn_worker(tmp_path)
    try:
        await eventually(lambda: _status(jobs_db, job) == "running", 30)
    finally:
        code, took, output = _stop(proc)
    assert code == 0, output
    assert took < budget(10), took  # GRACE_S 8 s plus the exit, on a loaded runner
    row = job_row(jobs_db.conn, job)
    assert (row["status"], row["attempt"]) == ("succeeded", 1)


async def test_worker_sigterm_long_job_released_uncounted(
    jobs_db: JobWorld, tmp_path: Path
) -> None:
    job = await _enqueue(jobs_db, sleep=120)
    proc = spawn_worker(tmp_path)
    try:
        await eventually(lambda: _status(jobs_db, job) == "running", 30)
    finally:
        code, took, output = _stop(proc)
    assert code == 0, output
    assert took < budget(10), took  # GRACE_S 8 s plus the exit, on a loaded runner
    row = job_row(jobs_db.conn, job)
    assert (row["status"], row["attempt"]) == ("queued", 0)
    nxt = spawn_worker(tmp_path, "--max-sleep", "0.1")
    try:
        await eventually(lambda: _status(jobs_db, job) == "succeeded", 30)
    finally:
        _stop(nxt)
    row = job_row(jobs_db.conn, job)
    assert row["attempt"] == 1
    assert row["result_json"]["pid"] == nxt.pid


async def test_worker_sigterm_exits_within_10s(jobs_db: JobWorld, tmp_path: Path) -> None:
    jobs = [
        await _enqueue(jobs_db, JobKind.BULK_IMPORT, sleep=120),
        await _enqueue(jobs_db, JobKind.LISTING_STATUS_SYNC, sleep=120),
    ]
    proc = spawn_worker(tmp_path)
    try:
        await eventually(lambda: all(_status(jobs_db, j) == "running" for j in jobs), 30)
    finally:
        code, took, output = _stop(proc)
    assert code == 0, output
    assert took < budget(10), took  # both queues drain in parallel inside the grace
    assert [_status(jobs_db, j) for j in jobs] == ["queued", "queued"]


async def test_worker_sigterm_unregisters_worker_row(jobs_db: JobWorld, tmp_path: Path) -> None:
    proc = spawn_worker(tmp_path)
    try:
        await eventually(
            lambda: count(jobs_db.conn, "SELECT count(*) FROM procrastinate_workers") == 2, 30
        )
    finally:
        code, _, output = _stop(proc)
    assert code == 0, output
    assert count(jobs_db.conn, "SELECT count(*) FROM procrastinate_workers") == 0
    assert "app_rw" not in output and "postgresql" not in output  # no DSN in any log line


async def _run_until(jw: JobWorld, check: Any) -> int:
    stop = asyncio.Event()
    runner = asyncio.create_task(main.run(stop))
    try:
        await eventually(check, 30)
    finally:
        stop.set()
    return await asyncio.wait_for(runner, 15)


async def test_worker_runs_both_queues_one_process(jobs_db: JobWorld, override_body: Any) -> None:
    seen: list[JobKind] = []

    async def body(ctx: JobContext) -> Outcome:
        seen.append(ctx.kind)
        return Outcome("succeeded")

    override_body(JobKind.BULK_IMPORT, body)
    override_body(JobKind.LISTING_STATUS_SYNC, body)
    a = await _enqueue(jobs_db, JobKind.BULK_IMPORT)
    b = await _enqueue(jobs_db, JobKind.LISTING_STATUS_SYNC)
    assert {job_row(jobs_db.conn, j)["queue"] for j in (a, b)} == {"default", "publish"}
    code = await _run_until(
        jobs_db, lambda: _status(jobs_db, a) == _status(jobs_db, b) == "succeeded"
    )
    assert code == 0
    assert sorted(seen) == sorted([JobKind.BULK_IMPORT, JobKind.LISTING_STATUS_SYNC])


async def test_worker_binds_no_port(jobs_db: JobWorld, monkeypatch: pytest.MonkeyPatch) -> None:
    listened: list[Any] = []
    real_listen = socket.socket.listen

    def listen(self: socket.socket, *args: Any) -> None:
        listened.append(self.family)
        real_listen(self, *args)

    monkeypatch.setattr(socket.socket, "listen", listen)
    code = await _run_until(
        jobs_db, lambda: count(jobs_db.conn, "SELECT count(*) FROM procrastinate_workers") == 2
    )
    assert code == 0
    assert listened == []


async def test_worker_inits_sentry_with_s11_options(
    jobs_db: JobWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sentry_sdk
    from app.core import observability
    from app.core.config import get_settings

    captured: dict[str, Any] = {}
    monkeypatch.setattr(sentry_sdk, "init", lambda **kwargs: captured.update(kwargs))
    monkeypatch.setenv("SENTRY_DSN", "https://public@o0.ingest.example.invalid/0")
    get_settings.cache_clear()
    code = await _run_until(
        jobs_db, lambda: count(jobs_db.conn, "SELECT count(*) FROM procrastinate_workers") == 2
    )
    assert code == 0
    assert captured["include_local_variables"] is False
    assert captured["send_default_pii"] is False
    assert captured["before_send"] is observability.before_send
    assert captured["max_request_body_size"] == "never"
