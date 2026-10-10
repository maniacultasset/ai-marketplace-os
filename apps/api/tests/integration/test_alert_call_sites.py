"""S23 T23 (AC11): the alert call sites. A dead letter from the wrapper (retries exhausted) or the
reaper (lease exhausted) emits one `job_dead_lettered` event after its commit, deduped per job,
capped per job kind; a fenced-out write emits nothing; a failing sink never fails the job. (The
refresh-failure alert is S15's `test_refresh_failure_reconnect`, updated in this step.)"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.workers import wrapper
from app.workers.errors import RetryableJobError
from app.workers.registry import JobContext, Outcome
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import JobKind

from .conftest import LOCAL_HOSTS
from .jobs_support import JobWorld, insert_job, job_row, jobs_db, override_body

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["jobs_db", "override_body"]
DSN = "https://k@o0.ingest.sentry.io/1"


@pytest.fixture
def sentry(sentry_events: Any) -> Any:
    from app.core.config import Settings
    from app.core.observability import init_sentry

    init_sentry(Settings(_env_file=None, app_env="local", app_role="worker", sentry_dsn=DSN))
    return sentry_events


def _flush() -> None:
    import sentry_sdk

    sentry_sdk.get_client().flush(timeout=2)


def _alerts(sentry: Any) -> list[dict[str, Any]]:
    _flush()
    return [e for e in sentry.events() if e.get("message") == "alert.job_dead_lettered"]


async def _retryable(ctx: JobContext) -> Outcome:
    raise RetryableJobError(ErrorCode.MARKETPLACE_UNAVAILABLE)


async def test_alert_events_job_dead_lettered_wrapper(
    jobs_db: JobWorld, override_body: Any, sentry: Any, log_lines: Any
) -> None:
    override_body(JobKind.BULK_IMPORT, _retryable)
    capture = log_lines()
    job = insert_job(jobs_db.conn, jobs_db.tenant("acme"), max_attempts=1)
    await wrapper.run_job(str(job))
    assert job_row(jobs_db.conn, job)["status"] == "dead_letter"
    lines = [line for line in capture.lines() if line["message"] == "alert.job_dead_lettered"]
    assert len(lines) == 1
    assert {k: lines[0][k] for k in ("tenant_id", "job_id", "job_kind", "error_code")} == {
        "tenant_id": str(jobs_db.tenant("acme")),
        "job_id": str(job),
        "job_kind": "bulk_import",
        "error_code": "RETRY_EXHAUSTED",
    }
    [event] = _alerts(sentry)
    assert event["fingerprint"] == ["alert", "job_dead_lettered", "bulk_import", "RETRY_EXHAUSTED"]


async def test_alert_events_job_dead_lettered_reaper(
    jobs_db: JobWorld, sentry: Any, log_lines: Any
) -> None:
    from app.workers.periodic import reap_once

    capture = log_lines()
    now = datetime.now(UTC)
    job = insert_job(
        jobs_db.conn,
        jobs_db.tenant("acme"),
        status="running",
        attempt=3,
        max_attempts=3,
        started_at=now - timedelta(hours=1),
        lease_expires_at=now - timedelta(minutes=1),
    )
    result = await reap_once()
    assert job in result.dead_lettered
    lines = [line for line in capture.lines() if line["message"] == "alert.job_dead_lettered"]
    assert len(lines) == 1 and lines[0]["error_code"] == "JOB_TIMEOUT"
    assert lines[0]["job_id"] == str(job)
    assert len(_alerts(sentry)) == 1


async def test_alert_after_commit_only(
    jobs_db: JobWorld, override_body: Any, sentry: Any, log_lines: Any
) -> None:
    """A fenced-out terminal write (the lease was reaped and another attempt owns the row) is
    dropped, and so is its alert."""
    acme = jobs_db.tenant("acme")
    job = insert_job(jobs_db.conn, acme, max_attempts=1)

    async def reaped_meanwhile(ctx: JobContext) -> Outcome:
        jobs_db.conn.execute(
            "UPDATE job_runs SET status = 'queued', lease_expires_at = NULL WHERE id = %s", [job]
        )
        raise RetryableJobError(ErrorCode.MARKETPLACE_UNAVAILABLE)

    override_body(JobKind.BULK_IMPORT, reaped_meanwhile)
    capture = log_lines()
    await wrapper.run_job(str(job))
    assert job_row(jobs_db.conn, job)["status"] == "queued"
    messages = [line["message"] for line in capture.lines()]
    assert "job.stale_write_dropped" in messages
    assert "alert.job_dead_lettered" not in messages
    assert _alerts(sentry) == []


async def test_alert_failure_never_fails_job(
    jobs_db: JobWorld,
    override_body: Any,
    log_lines: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slack unreachable and the Sentry client raising: the dead letter is still written and the
    task returns normally."""
    import sentry_sdk
    from app.core.config import get_settings

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("sentry down")

    monkeypatch.setattr(sentry_sdk, "capture_message", broken)
    monkeypatch.setenv("ALERT_SLACK_WEBHOOK", "https://hooks.slack.example/services/x")
    get_settings.cache_clear()
    override_body(JobKind.BULK_IMPORT, _retryable)
    capture = log_lines()
    job = insert_job(jobs_db.conn, jobs_db.tenant("acme"), max_attempts=1)
    await wrapper.run_job(str(job))
    get_settings.cache_clear()
    assert job_row(jobs_db.conn, job)["status"] == "dead_letter"
    messages = [line["message"] for line in capture.lines()]
    assert "alert.sentry_failed" in messages
    assert "alert.job_dead_lettered" in messages


async def test_alert_dead_letter_cap(jobs_db: JobWorld, override_body: Any, sentry: Any) -> None:
    """100 dead letters of one job kind in one window, through the wrapper: at most 20 Sentry
    events, one fingerprint; no pool connection is left checked out."""
    from app.db.session import get_engine

    override_body(JobKind.BULK_IMPORT, _retryable)
    acme = jobs_db.tenant("acme")
    for _ in range(100):
        job = insert_job(jobs_db.conn, acme, max_attempts=1)
        await wrapper.run_job(str(job))
    events = _alerts(sentry)
    assert 1 <= len(events) <= 20
    assert {tuple(e["fingerprint"]) for e in events} == {
        ("alert", "job_dead_lettered", "bulk_import", "RETRY_EXHAUSTED")
    }
    dead = jobs_db.conn.execute(
        "SELECT count(*) FROM job_runs WHERE status = 'dead_letter' AND tenant_id = %s", [acme]
    ).fetchone()
    assert dead == (100,)
    assert get_engine().pool.checkedout() == 0  # type: ignore[attr-defined]
