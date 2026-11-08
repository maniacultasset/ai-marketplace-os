"""SI12 T16-T17 (AC4, AC7, AC13): the size-chart read job through the real wrapper, on a fake
object store and the stub pipeline; its terminal and requeue hooks."""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from typing import Any

import pytest
from app.workers import size_chart_seam
from mkos_schemas.jobs import ModelCredentials
from mkos_schemas.size_chart_read import (
    SizeChartReadChart,
    SizeChartReadRequest,
    SizeChartReadResult,
)

from . import factories as f
from .conftest import LOCAL_HOSTS
from .images_support import FakeStorage, images_db, run_image_job
from .jobs_support import JobWorld, job_row
from .size_chart_support import chart_image, enqueue_read, read_state, use_read_storage

__all__ = ["images_db"]
pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStorage:
    fake = FakeStorage()
    use_read_storage(monkeypatch, fake)
    return fake


def _product(jw: JobWorld) -> tuple[uuid.UUID, uuid.UUID]:
    tenant = jw.tenant("acme")
    return tenant, f.product(jw.conn, tenant, category="tshirts")


def _snapshot(conn: Any, product_id: uuid.UUID) -> tuple[Any, ...]:
    row = conn.execute(
        "SELECT attributes, provenance, updated_at FROM products WHERE id = %s", [product_id]
    ).fetchone()
    assert row is not None
    listings = conn.execute(
        "SELECT count(*) FROM listings WHERE product_id = %s", [product_id]
    ).fetchone()
    return (*row, listings)


def _runs(conn: Any, job: uuid.UUID) -> list[dict[str, Any]]:
    cur = conn.execute("SELECT * FROM agent_runs WHERE job_id = %s", [job])
    names = [d.name for d in cur.description or ()]
    return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]


def _reserved(conn: Any, job: uuid.UUID) -> Decimal:
    row = conn.execute("SELECT cost_reserved_usd FROM job_runs WHERE id = %s", [job]).fetchone()
    assert row is not None
    return Decimal(row[0])


def _answer(monkeypatch: pytest.MonkeyPatch, change: Any) -> list[SizeChartReadRequest]:
    """The stub's answer, changed by `change(result, request)`; records each request."""
    from mkos_pipeline import read_size_chart as stub

    seen: list[SizeChartReadRequest] = []

    async def fake(request: SizeChartReadRequest, credentials: ModelCredentials) -> Any:
        seen.append(request)
        return change(await stub(request, credentials), request)

    monkeypatch.setattr(size_chart_seam, "call_read_size_chart", fake)
    return seen


# ---- the stub path ----


async def test_stub_read_stores_the_normalised_chart(
    images_db: JobWorld, store: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, p = _product(images_db)
    image, digest = chart_image(images_db.conn, store, tenant, p)
    before = _snapshot(images_db.conn, p)
    seen = _answer(monkeypatch, lambda result, _request: result)
    job = await enqueue_read(tenant, image)
    await run_image_job(job)
    assert job_row(images_db.conn, job)["status"] == "succeeded"
    state = read_state(images_db.conn, image)
    assert state is not None and state["status"] == "read"
    assert state["source_hash"] == digest and state["job_id"] == str(job)
    assert state["chart"] == {
        "unit": "cm",
        "basis": "garment",
        "rows": [
            {"size": "S", "chest": 96, "length": 104},
            {"size": "M", "chest": 101, "length": 106},
            {"size": "L", "chest": 106, "length": 108},
        ],
    }
    assert state["confidence"] == 0.9 and state["read_at"]
    assert len(seen) == 1 and seen[0].image.slot == "size_chart"
    assert seen[0].image.image_id == image and seen[0].category == "tshirts"
    runs = _runs(images_db.conn, job)
    assert len(runs) == 1 and runs[0]["tenant_id"] == tenant and runs[0]["product_id"] == p
    assert _reserved(images_db.conn, job) == 0
    assert _snapshot(images_db.conn, p) == before  # never a fact, provenance or listing


async def test_pipeline_without_the_entry_point_is_unavailable(
    images_db: JobWorld, store: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mkos_pipeline

    tenant, p = _product(images_db)
    image, _ = chart_image(images_db.conn, store, tenant, p)
    monkeypatch.delattr(mkos_pipeline, "read_size_chart")
    job = await enqueue_read(tenant, image)
    await run_image_job(job)
    state = read_state(images_db.conn, image)
    assert state is not None and state["status"] == "unavailable" and "chart" not in state
    assert _runs(images_db.conn, job) == []
    assert _reserved(images_db.conn, job) == 0


def _rows(result: SizeChartReadResult, rows: list[dict[str, Any]]) -> SizeChartReadResult:
    assert result.chart is not None
    return result.model_copy(update={"chart": result.chart.model_copy(update={"rows": rows})})


@pytest.mark.parametrize(
    "rows",
    [
        [{"size": f"S{i}", "chest": 90} for i in range(41)],
        [{"size": "M", "chest": 900}],
        [{"size": "M", "chest": 100, "neck_x": 12}],
        [{"size": "M\x00", "chest": 100}],
        [{"size": "M", "chest": 100}, {"size": "m", "chest": 101}],
    ],
    ids=["41_rows", "measure_900", "unknown_key", "nul_in_size", "duplicate_size"],
)
async def test_out_of_bounds_answers_are_unreadable(
    images_db: JobWorld,
    store: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
    rows: list[dict[str, Any]],
) -> None:
    tenant, p = _product(images_db)
    image, _ = chart_image(images_db.conn, store, tenant, p)
    if len(rows) > 40:
        # the seam model refuses 41 rows itself, so the answer is built past its validation

        def change(result: SizeChartReadResult, _r: Any) -> Any:
            assert result.chart is not None
            chart = SizeChartReadChart.model_construct(unit="cm", basis="garment", rows=rows)
            return result.model_copy(update={"chart": chart})

        _answer(monkeypatch, change)
    else:
        _answer(monkeypatch, lambda result, _r: _rows(result, rows))
    job = await enqueue_read(tenant, image)
    await run_image_job(job)
    state = read_state(images_db.conn, image)
    assert state is not None
    expected = "failed" if len(rows) > 40 else "unreadable"  # an invalid answer is untrusted
    assert state["status"] == expected and "chart" not in state


async def test_unreadable_answer(
    images_db: JobWorld, store: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, p = _product(images_db)
    image, _ = chart_image(images_db.conn, store, tenant, p)
    _answer(monkeypatch, lambda r, _q: r.model_copy(update={"chart": None, "unreadable": True}))
    job = await enqueue_read(tenant, image)
    await run_image_job(job)
    assert read_state(images_db.conn, image)["status"] == "unreadable"  # type: ignore[index]
    assert len(_runs(images_db.conn, job)) == 1


async def test_timeout_retries_then_fails_and_releases(
    images_db: JobWorld, store: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, p = _product(images_db)
    image, _ = chart_image(images_db.conn, store, tenant, p)
    calls: list[int] = []

    async def hang(request: Any, credentials: Any) -> Any:
        calls.append(request.attempt)
        raise TimeoutError

    monkeypatch.setattr(size_chart_seam, "call_read_size_chart", hang)
    job = await enqueue_read(tenant, image)
    runs = await run_image_job(job, max_runs=5)
    row = job_row(images_db.conn, job)
    assert row["status"] == "dead_letter", row["status"]
    assert runs == 3 and calls == [1, 2, 3]
    state = read_state(images_db.conn, image)
    assert state is not None and state["status"] == "failed" and state["job_id"] == str(job)
    # a dead letter keeps the hold counted: the timed-out call may have been billed (the
    # wrapper's retry-exhaustion rule, as SI7's timeout)
    assert _reserved(images_db.conn, job) == Decimal("0.02")


async def test_image_replaced_before_the_read_is_skipped(
    images_db: JobWorld, store: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, p = _product(images_db)
    old, _ = chart_image(images_db.conn, store, tenant, p)
    seen = _answer(monkeypatch, lambda r, _q: r)
    job = await enqueue_read(tenant, old)
    chart_image(images_db.conn, store, tenant, p)  # a newer chart takes over
    await run_image_job(job)
    assert job_row(images_db.conn, job)["status"] == "succeeded"
    assert seen == [] and read_state(images_db.conn, old) is None


async def test_image_replaced_mid_read_writes_nothing(
    images_db: JobWorld, store: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, p = _product(images_db)
    image, _ = chart_image(images_db.conn, store, tenant, p)

    def change(result: SizeChartReadResult, _r: Any) -> Any:
        images_db.conn.execute(
            "UPDATE images SET clean_content_hash = %s WHERE id = %s", ["f" * 64, image]
        )
        return result

    _answer(monkeypatch, change)
    job = await enqueue_read(tenant, image)
    await run_image_job(job)
    assert read_state(images_db.conn, image) is None
    assert job_row(images_db.conn, job)["result_json"]["status"] == "skipped"


async def test_wrong_tenant_image_is_skipped(
    images_db: JobWorld, store: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, p = _product(images_db)
    image, _ = chart_image(images_db.conn, store, tenant, p)
    rival = images_db.tenant("bharat")
    seen = _answer(monkeypatch, lambda r, _q: r)
    job = await enqueue_read(rival, image)  # a job of another tenant naming this image
    await run_image_job(job)
    assert seen == [] and read_state(images_db.conn, image) is None


async def test_foreign_record_is_untrusted(
    images_db: JobWorld, store: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, p = _product(images_db)
    image, _ = chart_image(images_db.conn, store, tenant, p)
    rival = images_db.tenant("bharat")

    def change(result: SizeChartReadResult, _r: Any) -> Any:
        record = result.agent_runs[0].model_copy(update={"tenant_id": rival})
        return result.model_copy(update={"agent_runs": [record]})

    _answer(monkeypatch, change)
    job = await enqueue_read(tenant, image)
    await run_image_job(job)
    assert job_row(images_db.conn, job)["status"] == "failed"
    assert _runs(images_db.conn, job) == []
    state = read_state(images_db.conn, image)
    assert state is not None and state["status"] == "failed"
    count = images_db.conn.execute(
        "SELECT count(*) FROM agent_runs WHERE tenant_id = %s", [rival]
    ).fetchone()
    assert count == (0,)


async def test_no_derivative_or_vision_is_handled(
    images_db: JobWorld, store: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, p = _product(images_db)
    image, _ = chart_image(images_db.conn, store, tenant, p, vision=False)
    seen = _answer(monkeypatch, lambda r, _q: r)
    job = await enqueue_read(tenant, image)
    await run_image_job(job)
    assert seen == []
    assert read_state(images_db.conn, image)["status"] == "failed"  # type: ignore[index]
    assert _reserved(images_db.conn, job) == 0


# ---- the hooks ----


async def test_old_jobs_hook_leaves_a_newer_jobs_result(
    images_db: JobWorld, store: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.db.session import unit_of_work
    from app.repositories.jobs import JobRunRepository
    from app.services.job_hooks import run_terminal_hook

    tenant, p = _product(images_db)
    image, _ = chart_image(images_db.conn, store, tenant, p)
    _answer(monkeypatch, lambda r, _q: r)
    old = await enqueue_read(tenant, image)
    newer = await enqueue_read(tenant, image)
    await run_image_job(newer)
    assert read_state(images_db.conn, image)["status"] == "read"  # type: ignore[index]
    async with unit_of_work(tenant) as session:
        cancelled = await JobRunRepository(session, tenant).cancel_queued(old)
        assert cancelled is not None
        await run_terminal_hook(session, cancelled, None)
    state = read_state(images_db.conn, image)
    assert state is not None and state["status"] == "read" and state["job_id"] == str(newer)


async def test_staff_requeue_refused(
    images_db: JobWorld, store: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.db.session import unit_of_work
    from app.repositories.jobs import JobRunRepository, NotEligibleError
    from app.services.job_hooks import run_requeue_hook

    async def hang(request: Any, credentials: Any) -> Any:
        await asyncio.sleep(0)
        raise TimeoutError

    tenant, p = _product(images_db)
    image, _ = chart_image(images_db.conn, store, tenant, p)
    monkeypatch.setattr(size_chart_seam, "call_read_size_chart", hang)
    job = await enqueue_read(tenant, image)
    await run_image_job(job, max_runs=5)
    assert job_row(images_db.conn, job)["status"] == "dead_letter"
    with pytest.raises(NotEligibleError):
        async with unit_of_work(tenant) as session:
            old, new = await JobRunRepository(session, tenant).requeue(job)
            await run_requeue_hook(session, old, new)
    count = images_db.conn.execute(
        "SELECT count(*) FROM job_runs WHERE parent_job_id = %s", [job]
    ).fetchone()
    assert count == (0,)
