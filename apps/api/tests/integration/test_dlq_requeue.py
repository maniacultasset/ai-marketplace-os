"""S13 A15 (DW9, AC6; CANON.md:61; DATABASE.md:312): a forced failure dead-letters; the staff CLI
lists dead letters across tenants read-only and requeues one as a NEW row with `parent_job_id`, a
fresh `correlation_id`, its queue entry, the requeue hook and a staff audit row."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from app import cli
from app.services import job_hooks
from app.workers import wrapper
from app.workers.errors import RetryableJobError
from app.workers.registry import JobContext, Outcome
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import JobKind

from .auth_support import add_tenant
from .conftest import LOCAL_HOSTS
from .jobs_support import JobWorld, count, insert_job, job_row, jobs_db, override_body, queue_row

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["jobs_db", "override_body"]
KIND = JobKind.BULK_IMPORT
CANARY = "payload-canary-7311"


def _dead(
    jw: JobWorld, slug: str = "acme", tenant: uuid.UUID | None = None, **extra: Any
) -> uuid.UUID:
    columns: dict[str, Any] = {
        "status": "dead_letter",
        "attempt": 3,
        "error_code": "RETRY_EXHAUSTED",
        "error_report_json": {"status": "failed", "error_code": "RETRY_EXHAUSTED"},
        "finished_at": datetime.now(UTC),
        "payload_json": {"note": CANARY},
        "created_by": None if tenant else jw.owner(slug),
        **extra,
    }
    return insert_job(jw.conn, tenant or jw.tenant(slug), **columns)


def _children(jw: JobWorld, parent: uuid.UUID) -> list[uuid.UUID]:
    rows = jw.conn.execute("SELECT id FROM job_runs WHERE parent_job_id = %s", [parent])
    return [r[0] for r in rows.fetchall()]


async def test_dlq_forced_failure_dead_letters(jobs_db: JobWorld, override_body: Any) -> None:
    async def body(ctx: JobContext) -> Outcome:
        raise RetryableJobError(ErrorCode.MARKETPLACE_UNAVAILABLE)

    override_body(KIND, body)
    job = insert_job(jobs_db.conn, jobs_db.tenant("acme"), max_attempts=1)
    await wrapper.run_job(str(job))
    row = job_row(jobs_db.conn, job)
    assert (row["status"], row["error_code"]) == ("dead_letter", "RETRY_EXHAUSTED")
    assert await cli.run_requeue(job, "ops@staff.test", "after the outage") == 0
    [child] = _children(jobs_db, job)
    assert job_row(jobs_db.conn, child)["status"] == "queued"


async def test_dlq_requeue_new_row(jobs_db: JobWorld, capsys: pytest.CaptureFixture[str]) -> None:
    old = _dead(jobs_db)
    assert await cli.run_requeue(old, "ops@staff.test", "fixed upstream") == 0
    [new] = _children(jobs_db, old)
    assert f"new job {new}" in capsys.readouterr().out
    a, b = job_row(jobs_db.conn, old), job_row(jobs_db.conn, new)
    for column in (
        "tenant_id",
        "kind",
        "entity_type",
        "entity_id",
        "listing_id",
        "payload_json",
        "created_by",
        "max_attempts",
        "batch_id",
    ):
        assert b[column] == a[column], column
    assert (b["status"], b["attempt"], b["parent_job_id"]) == ("queued", 0, old)
    assert b["correlation_id"] != a["correlation_id"] and b["idempotency_key"] is None
    assert b["request_id"] and b["request_id"] != a["request_id"]
    assert queue_row(jobs_db.conn, b["queue_job_id"])["args"] == {"job_run_id": str(new)}
    audit = jobs_db.conn.execute(
        "SELECT tenant_id, actor_type, actor_user_id, actor_email, entity_type, entity_id, "
        "reason, after, request_id FROM audit_log WHERE action = 'job.requeue'"
    ).fetchall()
    assert len(audit) == 1
    tenant, actor, user, email, entity, entity_id, reason, after, rid = audit[0]
    assert (tenant, actor, user, email) == (jobs_db.tenant("acme"), "staff", None, "ops@staff.test")
    assert (entity, entity_id, reason) == ("job_run", new, "fixed upstream")
    assert after == {"kind": "bulk_import", "status": "queued", "parent_job_id": str(old)}
    assert rid == b["request_id"]


async def test_dlq_requeue_old_row_unchanged(jobs_db: JobWorld) -> None:
    old = _dead(jobs_db)
    before = job_row(jobs_db.conn, old)
    assert await cli.run_requeue(old, "ops@staff.test", "r") == 0
    assert job_row(jobs_db.conn, old) == before


async def test_dlq_requeue_twice_exit_4(
    jobs_db: JobWorld, capsys: pytest.CaptureFixture[str]
) -> None:
    old = _dead(jobs_db)
    assert await cli.run_requeue(old, "ops@staff.test", "r") == 0
    assert await cli.run_requeue(old, "ops@staff.test", "r") == 4
    assert "already requeued" in capsys.readouterr().err
    assert len(_children(jobs_db, old)) == 1


@pytest.mark.parametrize("status", ["queued", "running", "succeeded", "cancelled"])
async def test_dlq_requeue_non_terminal_exit_4(jobs_db: JobWorld, status: str) -> None:
    extra: dict[str, Any] = {"status": status}
    if status == "running":
        extra.update(attempt=1, started_at=datetime.now(UTC), lease_expires_at=datetime.now(UTC))
    if status in ("succeeded", "cancelled"):
        extra.update(finished_at=datetime.now(UTC))
    job = insert_job(jobs_db.conn, jobs_db.tenant("acme"), **extra)
    assert await cli.run_requeue(job, "ops@staff.test", "r") == 4
    assert _children(jobs_db, job) == []


async def test_dlq_requeue_failed_is_eligible(jobs_db: JobWorld) -> None:
    old = _dead(jobs_db, status="failed", error_code="JOB_TIMEOUT")
    assert await cli.run_requeue(old, "ops@staff.test", "r") == 0


async def test_dlq_requeue_unknown_exit_4(
    jobs_db: JobWorld, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await cli.run_requeue(uuid.uuid4(), "ops@staff.test", "r") == 4
    assert "unknown job" in capsys.readouterr().err


async def test_dlq_requeue_suspended_exit_4(jobs_db: JobWorld) -> None:
    tenant = add_tenant(jobs_db.conn, "paused-co", status="suspended")
    old = _dead(jobs_db, tenant=tenant)
    assert await cli.run_requeue(old, "ops@staff.test", "r") == 4
    assert _children(jobs_db, old) == []
    assert count(jobs_db.conn, "SELECT count(*) FROM audit_log WHERE action = 'job.requeue'") == 0


async def test_dlq_list_reads_both_tenants_read_only(
    jobs_db: JobWorld, capsys: pytest.CaptureFixture[str]
) -> None:
    a, b = _dead(jobs_db), _dead(jobs_db, "bharat")
    insert_job(jobs_db.conn, jobs_db.tenant("acme"))  # a queued job is not listed
    rows_before = count(jobs_db.conn, "SELECT count(*) FROM job_runs")
    assert await cli.run_dead_letter_list(None, 100) == 0
    out = capsys.readouterr().out
    assert str(a) in out and str(b) in out and "acme" in out and "bharat" in out
    assert "2 job(s)" in out
    assert await cli.run_dead_letter_list("bharat", 100) == 0
    only = capsys.readouterr().out
    assert str(b) in only and str(a) not in only
    assert count(jobs_db.conn, "SELECT count(*) FROM job_runs") == rows_before


async def test_dlq_output_has_no_payload_or_secrets(
    jobs_db: JobWorld, capsys: pytest.CaptureFixture[str]
) -> None:
    old = _dead(jobs_db)
    await cli.run_dead_letter_list(None, 10)
    await cli.run_requeue(old, "ops@staff.test", "r")
    captured = capsys.readouterr()
    text = captured.out + captured.err
    for secret in (CANARY, "app_rw", "postgresql", "error_report", "password"):
        assert secret not in text, secret


async def test_dlq_requeue_audit_entity_allowlisted(jobs_db: JobWorld) -> None:
    allow = jobs_db.conn.execute("SELECT public.audit_allowlist('job_run')").fetchone()
    assert allow == (["kind", "status", "parent_job_id"],)
    old = _dead(jobs_db)
    assert await cli.run_requeue(old, "ops@staff.test", "r") == 0
    assert count(jobs_db.conn, "SELECT count(*) FROM audit_log WHERE entity_type = 'job_run'") == 1


async def test_dlq_requeue_calls_requeue_hook_once(
    jobs_db: JobWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[uuid.UUID, uuid.UUID, uuid.UUID | None]] = []

    async def hook(session: Any, old: Any, new: Any) -> None:
        calls.append((old.id, new.id, new.parent_job_id))

    monkeypatch.setitem(job_hooks.REQUEUE_HOOKS, KIND, hook)
    old = _dead(jobs_db)
    assert await cli.run_requeue(old, "ops@staff.test", "r") == 0
    [new] = _children(jobs_db, old)
    assert calls == [(old, new, old)]
