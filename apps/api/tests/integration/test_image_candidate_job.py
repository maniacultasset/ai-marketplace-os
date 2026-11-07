"""SI7 T11-T12: the candidate job through the real wrapper, on a fake object store.

Part 1 (T11): the zero-token kinds (`fix`, `size_chart`), the terminal and requeue hooks."""

from __future__ import annotations

import io
import uuid
from typing import Any

import pytest
from app.adapters.storage import StorageUnavailable
from app.services import image_runner
from app.services.image_keys import candidate_key
from app.services.image_runner import CandidateChildFailed
from PIL import Image as PILImage

from .candidates_support import (
    agent_run_count,
    candidate_row,
    chart_product,
    enqueue_candidate,
    photo,
    source_main,
    use_candidate_storage,
)
from .conftest import LOCAL_HOSTS
from .images_support import FakeStorage, images_db, run_image_job
from .jobs_support import JobWorld, job_row

__all__ = ["images_db"]
pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStorage:
    fake = FakeStorage()
    use_candidate_storage(monkeypatch, fake)
    return fake


async def _fix(jw: JobWorld, store: FakeStorage, **kw: Any) -> tuple[uuid.UUID, uuid.UUID]:
    tenant = jw.tenant("acme")
    p, src, digest = source_main(jw.conn, store, tenant, **kw)
    return await enqueue_candidate(tenant, p, src, digest)


async def test_fix_on_an_off_white_main_is_ready(images_db: JobWorld, store: FakeStorage) -> None:
    candidate, job = await _fix(images_db, store)
    await run_image_job(job)
    row = candidate_row(images_db.conn, candidate)
    assert row["status"] == "ready" and row["failure_reason"] is None
    tenant = images_db.tenant("acme")
    key = candidate_key(tenant, row["product_id"], candidate)
    assert row["storage_key"] == key and key in store.objects
    img = PILImage.open(io.BytesIO(store.objects[key][0]))
    assert img.format == "JPEG" and img.width == img.height >= 1000
    assert (row["width_px"], row["height_px"]) == img.size
    assert agent_run_count(images_db.conn, tenant) == 0
    assert job_row(images_db.conn, job)["status"] == "succeeded"


async def test_fix_too_small(images_db: JobWorld, store: FakeStorage) -> None:
    candidate, job = await _fix(images_db, store, clean=photo((600, 600)))
    await run_image_job(job)
    row = candidate_row(images_db.conn, candidate)
    assert (row["status"], row["failure_reason"]) == ("failed", "too_small")


async def test_size_chart_is_ready(images_db: JobWorld, store: FakeStorage) -> None:
    from app.services.image_candidates import size_chart_source

    tenant = images_db.tenant("acme")
    p = chart_product(images_db.conn, tenant)
    found = images_db.conn.execute("SELECT attributes FROM products WHERE id = %s", [p]).fetchone()
    assert found is not None
    source = size_chart_source(found[0])
    assert source is not None
    candidate, job = await enqueue_candidate(tenant, p, None, source.source_hash, kind="size_chart")
    await run_image_job(job)
    row = candidate_row(images_db.conn, candidate)
    assert row["status"] == "ready" and (row["width_px"], row["height_px"]) == (1500, 1500)
    assert agent_run_count(images_db.conn, tenant) == 0


async def test_size_chart_changed_since_create(images_db: JobWorld, store: FakeStorage) -> None:
    tenant = images_db.tenant("acme")
    p = chart_product(images_db.conn, tenant)
    candidate, job = await enqueue_candidate(tenant, p, None, "0" * 64, kind="size_chart")
    await run_image_job(job)
    row = candidate_row(images_db.conn, candidate)
    assert (row["status"], row["failure_reason"]) == ("failed", "source_unusable")


async def test_source_replaced_before_the_job(images_db: JobWorld, store: FakeStorage) -> None:
    candidate, job = await _fix(images_db, store)
    images_db.conn.execute(
        "UPDATE images SET clean_content_hash = %s WHERE id = ("
        "SELECT source_image_id FROM image_candidates WHERE id = %s)",
        ["b" * 64, candidate],
    )
    await run_image_job(job)
    row = candidate_row(images_db.conn, candidate)
    assert (row["status"], row["failure_reason"]) == ("failed", "source_unusable")


async def test_storage_outage_is_retried(images_db: JobWorld, store: FakeStorage) -> None:
    candidate, job = await _fix(images_db, store)
    real_get = store.get
    calls = {"n": 0}

    async def flaky(*a: Any, **k: Any) -> bytes:
        calls["n"] += 1
        if calls["n"] == 1:
            raise StorageUnavailable("down")
        return await real_get(*a, **k)

    store.get = flaky  # type: ignore[method-assign]
    runs = await run_image_job(job, max_runs=3)
    assert runs == 2
    assert candidate_row(images_db.conn, candidate)["status"] == "ready"


async def test_child_failure_on_every_attempt_is_model_failed_and_not_cached(
    images_db: JobWorld, store: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*a: Any, **k: Any) -> Any:
        raise CandidateChildFailed("timeout")

    monkeypatch.setattr(image_runner, "run_candidate_isolated", broken)
    tenant = images_db.tenant("acme")
    p, src, digest = source_main(images_db.conn, store, tenant)
    candidate, job = await enqueue_candidate(tenant, p, src, digest)
    await run_image_job(job, max_runs=3)
    row = candidate_row(images_db.conn, candidate)
    assert (row["status"], row["failure_reason"]) == ("failed", "model_failed")
    assert job_row(images_db.conn, job)["status"] == "dead_letter"
    again, _ = await enqueue_candidate(tenant, p, src, digest)  # not cached: a new row and job
    assert again != candidate


async def test_framed_output_failing_background_is_not_white(
    images_db: JobWorld, store: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.image_runner import CandidateReply

    async def verdict(*a: Any, **k: Any) -> CandidateReply:
        return CandidateReply(ok=False, reason="not_white")

    monkeypatch.setattr(image_runner, "run_candidate_isolated", verdict)
    candidate, job = await _fix(images_db, store)
    await run_image_job(job)
    row = candidate_row(images_db.conn, candidate)
    assert (row["status"], row["failure_reason"]) == ("failed", "not_white")


async def test_body_exception_is_model_failed(
    images_db: JobWorld, store: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(image_runner, "run_candidate_isolated", boom)
    candidate, job = await _fix(images_db, store)
    await run_image_job(job)
    row = candidate_row(images_db.conn, candidate)
    assert (row["status"], row["failure_reason"]) == ("failed", "model_failed")
    assert job_row(images_db.conn, job)["status"] == "failed"


async def test_cancel_is_model_failed(images_db: JobWorld, store: FakeStorage) -> None:
    from app.db.session import unit_of_work
    from app.repositories.jobs import JobRunRepository
    from app.services.job_hooks import run_terminal_hook

    candidate, job = await _fix(images_db, store)
    tenant = images_db.tenant("acme")
    async with unit_of_work(tenant) as session:
        cancelled = await JobRunRepository(session, tenant).cancel_queued(job)
        assert cancelled is not None
        await run_terminal_hook(session, cancelled, None)
    row = candidate_row(images_db.conn, candidate)
    assert (row["status"], row["failure_reason"]) == ("failed", "model_failed")


async def test_staff_requeue_refused(images_db: JobWorld, store: FakeStorage) -> None:
    from app.db.session import unit_of_work
    from app.repositories.jobs import JobRunRepository, NotEligibleError
    from app.services.job_hooks import run_requeue_hook

    candidate, job = await _fix(images_db, store)
    images_db.conn.execute(
        "UPDATE job_runs SET status = 'running', attempt = 3, started_at = now(), "
        "lease_expires_at = now() + interval '1 hour' WHERE id = %s",
        [job],
    )
    images_db.conn.execute(
        "UPDATE job_runs SET status = 'dead_letter', lease_expires_at = NULL, "
        "finished_at = now(), error_report_json = '{\"errors\": []}' WHERE id = %s",
        [job],
    )
    tenant = images_db.tenant("acme")
    with pytest.raises(NotEligibleError):
        async with unit_of_work(tenant) as session:  # the staff CLI: requeue, then the hook
            old, new = await JobRunRepository(session, tenant).requeue(job)
            await run_requeue_hook(session, old, new)
    count = images_db.conn.execute(
        "SELECT count(*) FROM job_runs WHERE parent_job_id = %s", [job]
    ).fetchone()
    assert count == (0,)  # the transaction rolled back
    assert candidate_row(images_db.conn, candidate)["status"] == "queued"


async def test_old_jobs_hook_leaves_a_newer_jobs_row(
    images_db: JobWorld, store: FakeStorage
) -> None:
    from app.db.session import unit_of_work
    from app.repositories.jobs import JobRunRepository
    from app.services.job_hooks import run_terminal_hook

    candidate, job = await _fix(images_db, store)
    newer = uuid.uuid4()
    images_db.conn.execute(
        "UPDATE image_candidates SET job_id = %s WHERE id = %s", [newer, candidate]
    )
    tenant = images_db.tenant("acme")
    async with unit_of_work(tenant) as session:
        cancelled = await JobRunRepository(session, tenant).cancel_queued(job)
        assert cancelled is not None
        await run_terminal_hook(session, cancelled, None)
    row = candidate_row(images_db.conn, candidate)
    assert row["status"] == "queued" and row["job_id"] == newer
