"""S13 A7 (DW10, AC10; S11 PLAN D9): a job runs under its origin request's `request_id`: its log
lines and the rows it writes carry it, the context does not leak into the next job, and a job
with none gets a fresh one that the row then names."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from app.core.logging import get_logger
from app.core.request_context import current_request_id, request_context
from app.db.session import unit_of_work
from app.repositories.jobs import EnqueueSpec, JobRunRepository
from app.workers import wrapper
from app.workers.registry import JobContext, Outcome
from mkos_schemas.enums import JobKind

from .conftest import LOCAL_HOSTS
from .jobs_support import JobWorld, insert_job, job_row, jobs_db, override_body

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["jobs_db", "override_body"]
KIND = JobKind.BULK_IMPORT


def _spec(jw: JobWorld) -> EnqueueSpec:
    return EnqueueSpec(
        kind=KIND, entity_type="import", entity_id=uuid.uuid4(), created_by=jw.owner("acme")
    )


async def test_worker_rebinds_request_id(
    jobs_db: JobWorld, override_body: Any, log_lines: Any
) -> None:
    capture = log_lines(log_level="INFO")
    tenant = jobs_db.tenant("acme")
    children: list[uuid.UUID] = []

    async def body(ctx: JobContext) -> Outcome:
        get_logger("tests.body").info("body.ran", job_run_id=str(ctx.job_run_id))
        async with unit_of_work(ctx.tenant_id) as session:
            child = await JobRunRepository(session, ctx.tenant_id).enqueue(_spec(jobs_db))
        children.append(child.job.id)
        return Outcome("succeeded")

    override_body(KIND, body)
    with request_context("s13-rid"):
        async with unit_of_work(tenant) as session:
            parent = await JobRunRepository(session, tenant).enqueue(_spec(jobs_db))
    assert job_row(jobs_db.conn, parent.job.id)["request_id"] == "s13-rid"
    capture.clear()
    assert current_request_id() is None
    await wrapper.run_job(str(parent.job.id))
    assert job_row(jobs_db.conn, children[0])["request_id"] == "s13-rid"
    lines = capture.lines()
    body_lines = [line for line in lines if line.get("message") == "body.ran"]
    assert len(body_lines) == 1
    assert body_lines[0]["request_id"] == "s13-rid"
    assert body_lines[0]["tenant_id"] == str(tenant)
    job_lines = [line for line in lines if str(line.get("message", "")).startswith("job.")]
    assert job_lines and all(line["request_id"] == "s13-rid" for line in job_lines)


async def test_worker_context_reset_between_jobs(jobs_db: JobWorld, override_body: Any) -> None:
    seen: list[str | None] = []

    async def body(ctx: JobContext) -> Outcome:
        seen.append(current_request_id())
        return Outcome("succeeded")

    override_body(KIND, body)
    tenant = jobs_db.tenant("acme")
    first = insert_job(jobs_db.conn, tenant, request_id="rid-one")
    second = insert_job(jobs_db.conn, tenant, request_id="rid-two")
    await wrapper.run_job(str(first))
    assert current_request_id() is None
    await wrapper.run_job(str(second))
    assert current_request_id() is None
    assert seen == ["rid-one", "rid-two"]


async def test_job_without_request_id_mints_one(jobs_db: JobWorld, override_body: Any) -> None:
    seen: list[str] = []

    async def body(ctx: JobContext) -> Outcome:
        seen.append(ctx.request_id)
        return Outcome("succeeded")

    override_body(KIND, body)
    job = insert_job(jobs_db.conn, jobs_db.tenant("acme"))  # a raw INSERT: request_id NULL
    assert job_row(jobs_db.conn, job)["request_id"] is None
    await wrapper.run_job(str(job))
    assert len(seen) == 1 and uuid.UUID(seen[0])
    assert job_row(jobs_db.conn, job)["request_id"] == seen[0]
