"""S13 A13 (DW5, AC7; CANON.md:60): `GET /api/v1/jobs/{job_id}` reads the envelope from `job_runs`
only, as app_rw through the real app; another tenant's job is 404."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from mkos_schemas.jobs import JobEnvelope

from .auth_support import PREFIX
from .conftest import LOCAL_HOSTS
from .jobs_support import JobWorld, as_member, bad_token, insert_job, jobs_db

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["jobs_db"]


def _url(job: uuid.UUID | str) -> str:
    return f"{PREFIX}/jobs/{job}"


@pytest.mark.endpoint("jobs_get", case="success")
@pytest.mark.parametrize("role", ["owner", "operator", "reviewer", "admin"])
async def test_jobs_get_success(jobs_db: JobWorld, api: httpx.AsyncClient, role: str) -> None:
    job = insert_job(jobs_db.conn, jobs_db.tenant("acme"), request_id="rid-a13")
    response = await api.get(_url(job), headers=as_member(jobs_db, f"{role}@acme.test"))
    assert response.status_code == 200, response.text
    env = JobEnvelope.model_validate(response.json())
    assert (env.job_id, env.kind.value, env.status.value) == (job, "bulk_import", "queued")
    assert (env.attempt, env.max_attempts, env.progress_pct) == (0, 3, 0)
    assert env.request_id == "rid-a13" and env.error is None and env.finished_at is None


@pytest.mark.endpoint("jobs_get", case="wrong_tenant")
async def test_jobs_get_other_tenant_404(jobs_db: JobWorld, api: httpx.AsyncClient) -> None:
    theirs = insert_job(jobs_db.conn, jobs_db.tenant("bharat"))
    response = await api.get(_url(theirs), headers=as_member(jobs_db, "owner@acme.test"))
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "NOT_FOUND"


@pytest.mark.endpoint("jobs_get", case="not_found")
async def test_jobs_get_unknown_404(jobs_db: JobWorld, api: httpx.AsyncClient) -> None:
    response = await api.get(_url(uuid.uuid4()), headers=as_member(jobs_db, "owner@acme.test"))
    assert response.status_code == 404


@pytest.mark.endpoint("jobs_get", case="validation")
async def test_jobs_get_bad_uuid_422(jobs_db: JobWorld, api: httpx.AsyncClient) -> None:
    response = await api.get(_url("not-a-uuid"), headers=as_member(jobs_db, "owner@acme.test"))
    assert response.status_code == 422
    assert response.json()["error"]["type"] == "VALIDATION_FAILED"


@pytest.mark.endpoint("jobs_get", case="missing_auth")
async def test_jobs_get_missing_auth_401(jobs_db: JobWorld, api: httpx.AsyncClient) -> None:
    job = insert_job(jobs_db.conn, jobs_db.tenant("acme"))
    assert (await api.get(_url(job))).status_code == 401


@pytest.mark.endpoint("jobs_get", case="invalid_token", variant="expired")
@pytest.mark.endpoint("jobs_get", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("jobs_get", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_jobs_get_invalid_token_401(
    jobs_db: JobWorld, api: httpx.AsyncClient, variant: str
) -> None:
    job = insert_job(jobs_db.conn, jobs_db.tenant("acme"))
    response = await api.get(_url(job), headers=bad_token(jobs_db, variant))
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "UNAUTHENTICATED"


async def test_jobs_get_deactivated_membership_401(
    jobs_db: JobWorld, api: httpx.AsyncClient
) -> None:
    job = insert_job(jobs_db.conn, jobs_db.tenant("acme"))
    headers = as_member(jobs_db, "operator@acme.test")
    jobs_db.conn.execute(
        "UPDATE tenant_memberships SET is_active = false, deactivated_at = now() "
        "WHERE user_id = %s",
        [jobs_db.world.user("operator@acme.test")],
    )
    assert (await api.get(_url(job), headers=headers)).status_code == 401


async def test_job_envelope_from_db(jobs_db: JobWorld, api: httpx.AsyncClient) -> None:
    from app.db.session import unit_of_work
    from app.repositories.jobs import EnqueueSpec, JobRunRepository
    from mkos_schemas.enums import JobKind

    tenant = jobs_db.tenant("acme")
    async with unit_of_work(tenant) as session:
        done = await JobRunRepository(session, tenant).enqueue(
            EnqueueSpec(JobKind.BULK_IMPORT, "import", uuid.uuid4(), jobs_db.owner("acme"))
        )
    headers = as_member(jobs_db, "owner@acme.test")
    before = (await api.get(_url(done.job.id), headers=headers)).json()
    jobs_db.conn.execute("TRUNCATE procrastinate_jobs CASCADE")
    after = (await api.get(_url(done.job.id), headers=headers)).json()
    assert before == after


async def test_job_envelope_import_progress_from_result_json(
    jobs_db: JobWorld, api: httpx.AsyncClient
) -> None:
    import_id = uuid.uuid4()
    job = insert_job(
        jobs_db.conn,
        jobs_db.tenant("acme"),
        status="running",
        attempt=1,
        started_at="2026-10-07T00:00:00Z",
        lease_expires_at="2099-01-01T00:00:00Z",
        result_json={"import_id": str(import_id), "progress_pct": 40},
    )
    body: dict[str, Any] = (
        await api.get(_url(job), headers=as_member(jobs_db, "owner@acme.test"))
    ).json()
    assert body["progress_pct"] == 40
    assert body["result_url"] == f"{PREFIX}/imports/{import_id}"
    assert body["status"] == "running" and body["started_at"] is not None


async def test_job_envelope_error_report_on_failed(
    jobs_db: JobWorld, api: httpx.AsyncClient
) -> None:
    from app.workers.errors import system_report
    from mkos_schemas.codes import ErrorCode

    report = system_report(
        ErrorCode.JOB_TIMEOUT,
        "the job exceeded its 2 s timeout",
        attempt=1,
        max_attempts=3,
        request_id="rid-x",
    )
    job = insert_job(
        jobs_db.conn,
        jobs_db.tenant("acme"),
        status="failed",
        attempt=1,
        finished_at="2026-10-07T00:00:00Z",
        error_code="JOB_TIMEOUT",
        error_report_json=report.model_dump(mode="json"),
    )
    env = JobEnvelope.model_validate(
        (await api.get(_url(job), headers=as_member(jobs_db, "owner@acme.test"))).json()
    )
    assert env.status.value == "failed" and env.error == report
    assert env.request_id  # a raw row with no request_id shows its correlation id
