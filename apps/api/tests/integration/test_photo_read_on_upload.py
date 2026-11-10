"""V1.1: the photo read on upload, as app_rw through the real wrapper and the stub pipeline.

A checked front, back or close-up photo queues one read per product and photo set; the read uses
the SI3 targets, screening and guarded store, records its set like a generation, and a set read
once is never paid for again (not on upload, not at the next generation)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import pytest

from .conftest import LOCAL_HOSTS
from .photo_support import OPERATOR, PhotoWorld, build_photo_world, generate, spy_pipeline

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)


def _switch(monkeypatch: pytest.MonkeyPatch, *, on: bool) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("PHOTO_READ_ON_UPLOAD_ENABLED", "true" if on else "false")
    get_settings.cache_clear()


@pytest.fixture
async def pw(auth_db: Any, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[PhotoWorld]:
    from app.db.queue import get_queue_app
    from app.workers import photo_facts_read

    get_queue_app.cache_clear()
    world = await build_photo_world(auth_db, monkeypatch)
    monkeypatch.setattr(photo_facts_read, "get_storage", lambda: world.store)
    _switch(monkeypatch, on=True)
    yield world
    get_queue_app.cache_clear()


async def _queue(pw: PhotoWorld, image_id: uuid.UUID) -> uuid.UUID | None:
    from app.repositories.uow import tenant_uow
    from app.services.photo_read_upload import queue_photo_read

    async with tenant_uow(pw.tenant) as uow:
        return await queue_photo_read(uow, image_id)


async def _run(job_id: uuid.UUID) -> None:
    from app.workers import wrapper
    from app.workers.tasks import register_bodies

    register_bodies()
    await wrapper.run_job(str(job_id))


async def _latest_set(pw: PhotoWorld) -> Any:
    from app.repositories.uow import tenant_uow

    async with tenant_uow(pw.tenant) as uow:
        return await uow.job_runs.latest_photo_set(pw.product_id)


async def _reading(pw: PhotoWorld) -> bool:
    from app.services import products

    view = await products.get_product(pw.world.principal(OPERATOR), pw.product_id)
    return view.photo_reading


def _upload_jobs(pw: PhotoWorld) -> list[uuid.UUID]:
    rows = pw.conn.execute(
        "SELECT id FROM job_runs WHERE entity_type = 'photo_facts_read' AND entity_id = %s",
        [pw.product_id],
    ).fetchall()
    return [r[0] for r in rows]


async def test_front_photo_read_writes_unconfirmed_values_once(pw: PhotoWorld) -> None:
    job = await _queue(pw, pw.images["main"])
    assert job is not None
    assert await _queue(pw, pw.images["main"]) == job  # the same set: the same job
    assert await _reading(pw) is True
    await _run(job)
    row = pw.job(job)
    assert row["status"] == "succeeded"
    assert row["kind"] == "image_compliance" and row["entity_type"] == "photo_facts_read"
    result = row["result_json"]
    assert result["status"] == "read" and result["photo_set"] == pw.sent_set()
    assert result["photo_read"]["status"] == "read" and result["photo_read"]["written"] >= 1
    written = [
        k for k, v in pw.attributes().items() if isinstance(v, dict) and v.get("source") == "ai"
    ]
    assert len(written) == result["photo_read"]["written"]
    for key in written:  # unconfirmed suggestions only, citing a product photo
        fact = pw.attributes()[key]
        assert fact["captured_via"] == "agent" and fact["evidence_ref"].startswith("image:")
    records = pw.photo_records(job)
    assert len(records) == 1 and records[0]["product_id"] == pw.product_id
    assert Decimal(row["cost_reserved_usd"]) == 0
    assert len(pw.audit("product.fact_updated")) == 1
    assert await _reading(pw) is False
    # read once: a later upload check of the same set and the next generation make no call
    assert await _queue(pw, pw.images["main"]) is None
    assert await _latest_set(pw) == tuple(tuple(p) for p in pw.sent_set())


async def test_next_generation_skips_the_set_read_on_upload(
    pw: PhotoWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = await _queue(pw, pw.images["main"])
    assert job is not None
    await _run(job)
    spy_pipeline(pw, monkeypatch)
    await generate(pw, None)
    assert len(pw.seen) == 1 and pw.seen[0].photo_targets == []


async def test_a_new_back_photo_reads_the_new_set(pw: PhotoWorld) -> None:
    first = await _queue(pw, pw.images["main"])
    assert first is not None
    await _run(first)
    back = pw.add_image("secondary")
    pw.conn.execute("UPDATE images SET role = 'back' WHERE id = %s", [back])
    second = await _queue(pw, back)
    assert second is not None and second != first
    await _run(second)
    assert pw.job(second)["result_json"]["photo_set"] == pw.sent_set()


async def test_only_front_back_and_close_up_photos_queue_a_read(pw: PhotoWorld) -> None:
    for slot in ("secondary", "lifestyle", "packaging_label"):
        assert await _queue(pw, pw.images[slot]) is None
    assert _upload_jobs(pw) == []


async def test_switch_off_queues_nothing_and_a_queued_read_makes_no_call(
    pw: PhotoWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = await _queue(pw, pw.images["main"])
    assert job is not None
    _switch(monkeypatch, on=False)
    assert await _queue(pw, pw.images["main"]) is None
    await _run(job)
    assert pw.job(job)["result_json"]["status"] == "skipped"
    assert pw.photo_records(job) == [] and await _latest_set(pw) is None


async def test_pipeline_without_the_entry_point_is_unavailable(
    pw: PhotoWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mkos_pipeline

    monkeypatch.delattr(mkos_pipeline, "read_photo_facts")
    job = await _queue(pw, pw.images["main"])
    assert job is not None
    await _run(job)
    row = pw.job(job)
    assert row["result_json"]["status"] == "unavailable"
    assert pw.photo_records(job) == [] and Decimal(row["cost_reserved_usd"]) == 0
    assert await _latest_set(pw) is None  # the next generation still reads the photos


async def test_budget_refusal_makes_no_call_and_records_no_set(
    pw: PhotoWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.workers import budget

    async def refuse(*_a: Any, **_kw: Any) -> bool:
        raise budget.BudgetRefused("tenant")

    monkeypatch.setattr(budget, "reserve", refuse)
    job = await _queue(pw, pw.images["main"])
    assert job is not None
    await _run(job)
    assert pw.job(job)["result_json"]["status"] == "budget"
    assert pw.photo_records(job) == [] and await _latest_set(pw) is None
