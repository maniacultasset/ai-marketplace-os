"""S26 T13 (AC 5, DW1): failure injection into the job spine, the pipeline, images and the DB.

Each fault ends in a documented state with one `ErrorReport` and no duplicate write: the same
Idempotency-Key twice is one job and one marketplace write; two publish jobs on one listing make
one write; a worker that dies after the write is reaped and reads before it retries; a pipeline
timeout or a malformed pipeline result stores no version; a truncated image is `CORRUPT` and
blocks the approval; a terminated DB backend mid-job is retried (3-attempt kinds) or fails once
(generation, `max_attempts` 1) with the reservation kept.
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .failure_support import AmzWorld, amz
from .generation_support import GenWorld, gen_world, make_generating
from .publish_support import PUBLISH_URL, approved, publish_one, run_job

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["amz", "gen_world"]
OPERATOR = "operator@acme.test"


async def _run(job_id: Any) -> str | None:
    try:
        await run_job(uuid.UUID(str(job_id)))
    except Exception as exc:  # the wrapper's RetryLater, or the test's own crash
        return type(exc).__name__
    return None


def _versions(world: GenWorld, listing_id: Any) -> int:
    return int(
        world.scalar("SELECT count(*) FROM listing_versions WHERE listing_id = %s", [listing_id])
    )


async def test_failure_injection_duplicate_publish_same_key_one_write(
    api: httpx.AsyncClient, amz: AmzWorld
) -> None:
    made = await approved(amz.pub)
    other = await approved(amz.pub)
    key = str(uuid.uuid4())
    body = {"items": [{"listing_id": str(made.listing_id)}]}
    headers = amz.world.headers(OPERATOR, key)
    first = await api.post(PUBLISH_URL, headers=headers, json=body)
    again = await api.post(PUBLISH_URL, headers=headers, json=body)
    assert first.status_code == again.status_code == 202, again.text
    assert first.json() == again.json()
    changed = await api.post(
        PUBLISH_URL, headers=headers, json={"items": [{"listing_id": str(other.listing_id)}]}
    )
    assert changed.status_code == 409, changed.text
    assert changed.json()["error"]["type"] == "IDEMPOTENCY_KEY_REUSED"
    (item,) = first.json()["items"]
    await _run(item["job_id"])
    jobs = amz.pub.scalar(
        "SELECT count(*) FROM job_runs WHERE listing_id = %s AND kind = 'listing_publish'",
        [made.listing_id],
    )
    assert jobs == 1
    assert amz.state.submissions == 1


async def test_failure_injection_two_jobs_one_listing_one_proceeds(
    api: httpx.AsyncClient, amz: AmzWorld
) -> None:
    made = await approved(amz.pub)
    first, second = await asyncio.gather(
        publish_one_raw(api, amz, made), publish_one_raw(api, amz, made)
    )
    accepted = [i for i in (first, second) if i["http_status"] == 202]
    assert len(accepted) >= 1, (first, second)
    for item in accepted:
        await _run(item["job_id"])
    refused = [i for i in (first, second) if i["http_status"] != 202]
    for item in refused:
        assert item["http_status"] == 409 and item["job_id"] is None, item
    assert amz.state.submissions == 1
    assert amz.pub.listing(made.listing_id)["status"] == "published"


async def publish_one_raw(api: httpx.AsyncClient, aw: AmzWorld, made: Any) -> dict[str, Any]:
    response = await api.post(
        PUBLISH_URL,
        headers=aw.world.headers(OPERATOR),
        json={"items": [{"listing_id": str(made.listing_id)}]},
    )
    assert response.status_code == 202, response.text
    (item,) = response.json()["items"]
    return item  # type: ignore[no-any-return]


class Killed(BaseException):
    """Stands in for SIGKILL inside the process: nothing after it runs (not an Exception, so no
    handler in the job body can turn it into an outcome)."""


async def test_failure_injection_worker_kill9_reaper_one_write(
    api: httpx.AsyncClient, amz: AmzWorld
) -> None:
    """The worker dies after the marketplace applied the write, before the reply is recorded
    (the pending submission is committed); the reaper requeues the job, which reads first and
    writes nothing more."""
    from app.workers.periodic import reap_once
    from tests.support.fault_transport import FaultTransport

    made = await approved(amz.pub)
    amz.transport = FaultTransport(_asgi(amz), after_write=Killed())  # type: ignore[arg-type]
    item = await publish_one(api, amz.pub, made)
    with pytest.raises(Killed):
        await run_job(uuid.UUID(item["job_id"]))
    (pending,) = amz.pub.submissions(made.listing_id)
    assert pending["responded_at"] is None
    assert amz.state.submissions == 1
    amz.transport = None
    amz.pub.conn.execute(
        "UPDATE job_runs SET lease_expires_at = now() - interval '1 second' WHERE id = %s",
        [item["job_id"]],
    )
    reaped = await reap_once()
    assert uuid.UUID(item["job_id"]) in reaped.requeued
    assert await _run(item["job_id"]) is None
    (sub,) = amz.pub.submissions(made.listing_id)
    assert sub["id"] == pending["id"] and sub["outcome"] == "accepted"
    assert amz.state.submissions == 1
    assert amz.pub.listing(made.listing_id)["status"] == "published"


def _asgi(aw: AmzWorld) -> httpx.ASGITransport:
    return httpx.ASGITransport(aw.app)


def _short_generation_timeout(monkeypatch: pytest.MonkeyPatch, seconds: int) -> None:
    from app.workers import wrapper
    from app.workers.policy import POLICIES
    from mkos_schemas.enums import JobKind

    policies = dict(POLICIES)
    policies[JobKind.LISTING_GENERATE] = dataclasses.replace(
        POLICIES[JobKind.LISTING_GENERATE], timeout_s=seconds
    )
    monkeypatch.setattr(wrapper, "POLICIES", policies)


async def test_failure_injection_stub_timeout_job_timeout_no_version(
    gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    _short_generation_timeout(monkeypatch, 1)
    accepted = await make_generating(gen_world, instructions="stub:timeout")
    assert _versions(gen_world, accepted.listing_id) == 0
    await _run(accepted.job_id)
    job = gen_world.conn.execute(
        "SELECT status, error_code, error_report_json, cost_reserved_usd FROM job_runs "
        "WHERE id = %s",
        [accepted.job_id],
    ).fetchone()
    assert job is not None
    assert (job[0], job[1]) == ("failed", "JOB_TIMEOUT"), job
    assert job[2]["error_code"] == "JOB_TIMEOUT"
    assert _versions(gen_world, accepted.listing_id) == 0
    row = gen_world.listing(accepted.listing_id)
    assert (row["status"], row["status_reason"]) == ("failed", "generation_failed")


async def test_failure_injection_stub_timeout_shipped_policy_job_timeout(
    gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S26 fix round 1: under the shipped policy (300 s job timeout) the stub's model-deadline
    TimeoutError itself ends `JOB_TIMEOUT` with no version, not `INTERNAL_ERROR`. The stub's
    wait (LLM_TIMEOUT_SECONDS + 1 s) is made instant, only inside the stub module."""
    import types

    import mkos_pipeline.graph as graph

    async def instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(graph, "asyncio", types.SimpleNamespace(sleep=instant))
    accepted = await make_generating(gen_world, instructions="stub:timeout")
    await _run(accepted.job_id)
    job = gen_world.conn.execute(
        "SELECT status, error_code, error_report_json FROM job_runs WHERE id = %s",
        [accepted.job_id],
    ).fetchone()
    assert job is not None
    assert (job[0], job[1]) == ("failed", "JOB_TIMEOUT"), job
    assert job[2]["error_code"] == "JOB_TIMEOUT"
    assert _versions(gen_world, accepted.listing_id) == 0
    row = gen_world.listing(accepted.listing_id)
    assert (row["status"], row["status_reason"]) == ("failed", "generation_failed")


async def test_failure_injection_stub_malformed_internal_error(gen_world: GenWorld) -> None:
    accepted = await make_generating(gen_world, instructions="stub:malformed")
    await _run(accepted.job_id)
    job = gen_world.conn.execute(
        "SELECT status, error_code, error_report_json FROM job_runs WHERE id = %s",
        [accepted.job_id],
    ).fetchone()
    assert job is not None
    assert (job[0], job[1]) == ("failed", "INTERNAL_ERROR"), job
    assert {e["source"] for e in job[2]["errors"]} == {"system"}
    assert _versions(gen_world, accepted.listing_id) == 0
    row = gen_world.listing(accepted.listing_id)
    assert (row["status"], row["status_reason"]) == ("failed", "generation_failed")


async def test_failure_injection_corrupt_image_corrupt(
    api: httpx.AsyncClient, gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.integration.images_support import (
        FakeStorage,
        enqueue_image_job,
        image_db_row,
        image_row,
        picture,
        run_image_job,
        use_storage,
    )
    from tests.integration.review_support import generated, item, only_item, post_approve
    from tests.integration.test_approve_image_gate import _with_images

    store = FakeStorage()
    use_storage(monkeypatch, store)
    made = await generated(gen_world, OPERATOR)
    acme = gen_world.tenant("acme")
    product = gen_world.listing(made.listing_id)["product_id"]
    image_id = image_row(gen_world.conn, acme, product, slot="main")
    key = image_db_row(gen_world.conn, image_id)["storage_key"]
    store.objects[key] = (picture(1000, 1000)[:600], "image/jpeg", datetime.now(UTC))
    await run_image_job(await enqueue_image_job(acme, image_id), max_runs=3)
    row = image_db_row(gen_world.conn, image_id)
    assert row["compliance_status"] == "failed"
    for report in row["image_report_json"].values():
        assert [i["code"] for i in report["issues"]] == ["CORRUPT"]
    with_image = _with_images(gen_world, made, [image_id])
    before = gen_world.listing(made.listing_id)
    result = only_item(await post_approve(api, gen_world, "reviewer@acme.test", [item(with_image)]))
    assert result["http_status"] == 422, result
    assert result["error"]["error_code"] == "IMAGE_NON_COMPLIANT"
    assert any(e["field"].startswith("images[") for e in result["error"]["errors"])
    after = gen_world.listing(made.listing_id)
    assert after["approved_version_id"] == before["approved_version_id"]


SLEEP_SQL = "SELECT pg_sleep(30) /* s26-terminate-me */"


def _terminate_marked(conn: Any) -> int:
    """`pg_terminate_backend` the app backend running the marked statement, in this database
    only (filtered by `current_database()`, so another suite's database is never touched)."""
    rows = conn.execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE datname = current_database() AND pid <> pg_backend_pid() "
        "AND query LIKE %s",
        ["%s26-terminate-me%"],
    ).fetchall()
    return len(rows)


async def _wait_marked(conn: Any, timeout_s: float = 60.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        found = conn.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
            "AND pid <> pg_backend_pid() AND query LIKE %s AND state = 'active'",
            ["%s26-terminate-me%"],
        ).fetchone()
        if found and found[0]:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the marked statement never started")


async def test_failure_injection_pg_terminate_backend_generation_fails_once(
    gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the pipeline's `retrieve_brand_chunks`, a port statement is running when the test
    terminates its backend. Generation has max_attempts 1: the job ends `failed` with one
    ErrorReport, no version, attempt 1, and the reservation kept (S18 CP10); no stub agent-run
    row is written for the crashed call (DATABASE open #19)."""
    from app.db.session import get_engine
    from app.services import pipeline_deps
    from sqlalchemy import text

    original = pipeline_deps.BackendPipelineDeps.retrieve_brand_chunks

    async def held(self: Any, *args: Any, **kwargs: Any) -> Any:
        out = await original(self, *args, **kwargs)
        async with get_engine().connect() as conn:
            await conn.execute(text(SLEEP_SQL))
        return out

    monkeypatch.setattr(pipeline_deps.BackendPipelineDeps, "retrieve_brand_chunks", held)
    accepted = await make_generating(gen_world)
    task = asyncio.create_task(_run(accepted.job_id))
    await _wait_marked(gen_world.conn)
    assert _terminate_marked(gen_world.conn) == 1
    await asyncio.wait_for(task, 120)
    job = gen_world.conn.execute(
        "SELECT status, error_report_json, cost_reserved_usd, attempt, error_code FROM job_runs "
        "WHERE id = %s",
        [accepted.job_id],
    ).fetchone()
    assert job is not None
    assert (job[0], job[4]) == ("failed", "INTERNAL_ERROR"), job
    assert job[1] is not None and job[1]["status"] == "failed"
    assert job[3] == 1
    assert job[2] is not None and job[2] > 0  # the reservation is kept, not zeroed
    assert _versions(gen_world, accepted.listing_id) == 0
    row = gen_world.listing(accepted.listing_id)
    assert (row["status"], row["status_reason"]) == ("failed", "generation_failed")
    stub_rows = gen_world.scalar(
        "SELECT count(*) FROM agent_runs WHERE job_id = %s AND model_used = 'stub'",
        [accepted.job_id],
    )
    assert stub_rows == 0


async def test_failure_injection_pg_terminate_backend_three_attempt_kind_retried_no_duplicate_rows(
    api: httpx.AsyncClient, amz: AmzWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A publish job (max_attempts 3) whose backend is terminated mid-statement before the
    write is retried: the retry reads before it writes, and the marketplace sees one write and
    the ledger one submission."""
    from app.db.session import get_engine
    from app.workers import publish as publish_worker
    from sqlalchemy import text

    made = await approved(amz.pub)
    original = publish_worker._read
    fired: list[int] = []

    async def held(ctx: Any, payload: Any) -> Any:
        if not fired:
            fired.append(1)
            async with get_engine().connect() as conn:
                await conn.execute(text(SLEEP_SQL))
        return await original(ctx, payload)

    monkeypatch.setattr(publish_worker, "_read", held)
    item = await publish_one(api, amz.pub, made)
    task = asyncio.create_task(_run(item["job_id"]))
    await _wait_marked(amz.pub.conn)
    assert _terminate_marked(amz.pub.conn) == 1
    first = await asyncio.wait_for(task, 120)
    assert first in ("RetryLater", None), first
    job = amz.pub.job(item["job_id"])
    if job["status"] != "succeeded":
        assert job["status"] in ("queued", "running"), job
        assert await _run(item["job_id"]) in (None, "RetryLater")
    assert amz.pub.job(item["job_id"])["status"] == "succeeded"
    assert len(amz.pub.submissions(made.listing_id)) == 1
    assert amz.state.submissions == 1
