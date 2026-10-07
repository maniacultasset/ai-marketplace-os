"""S13 A4 (AC3, AC12): the worker names its job through `jr_by_id` as app_rw, before any tenant
GUC, and sees that one row only; the GUC does not outlive the transaction."""

from __future__ import annotations

import uuid

import pytest
from app.db.job_context import resolve_job
from app.db.session import get_engine
from sqlalchemy import text

from .conftest import LOCAL_HOSTS
from .jobs_support import JobWorld, insert_job, jobs_db

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["jobs_db"]


async def test_resolve_job_via_jr_by_id(jobs_db: JobWorld) -> None:
    tenant = jobs_db.tenant("acme")
    job = insert_job(jobs_db.conn, tenant, request_id="rid-a4")
    head = await resolve_job(job)
    assert head is not None
    assert (head.tenant_id, head.kind, head.request_id) == (tenant, "bulk_import", "rid-a4")


async def test_resolve_job_runs_as_app_rw(jobs_db: JobWorld) -> None:
    async with get_engine().connect() as conn:
        assert (await conn.execute(text("SELECT current_user"))).scalar() == "app_rw"


async def test_resolve_job_other_id_not_visible(jobs_db: JobWorld) -> None:
    x = insert_job(jobs_db.conn, jobs_db.tenant("acme"))
    y = insert_job(jobs_db.conn, jobs_db.tenant("bharat"))
    async with get_engine().connect() as conn, conn.begin():
        await conn.execute(text("SELECT set_config('app.job_id', :x, true)"), {"x": str(x)})
        seen = (await conn.execute(text("SELECT id FROM job_runs WHERE id = :y"), {"y": y})).all()
        everything = (await conn.execute(text("SELECT id FROM job_runs"))).scalars().all()
    assert seen == []
    assert everything == [x]


async def test_resolve_unknown_none(jobs_db: JobWorld) -> None:
    assert await resolve_job(uuid.uuid4()) is None


async def test_resolve_refuses_non_uuid(jobs_db: JobWorld) -> None:
    with pytest.raises(ValueError, match="UUID"):
        await resolve_job("not-a-uuid")  # type: ignore[arg-type]


async def test_job_guc_empty_after_commit(jobs_db: JobWorld) -> None:
    job = insert_job(jobs_db.conn, jobs_db.tenant("acme"))
    # one pooled connection, so the next checkout is the connection resolve_job used
    engine = get_engine()
    await engine.dispose()
    assert await resolve_job(job) is not None
    async with engine.connect() as conn:
        value = (await conn.execute(text("SELECT current_setting('app.job_id', true)"))).scalar()
        rows = (await conn.execute(text("SELECT count(*) FROM job_runs"))).scalar()
    assert value in (None, "")
    assert rows == 0
