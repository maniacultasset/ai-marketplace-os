"""SI14 T13 (AC5): the classification job through the real wrapper, on a fake object store and
the stub pipeline (`classify_product`)."""

from __future__ import annotations

import asyncio
import hashlib
import io
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import mkos_pipeline
import pytest
from mkos_pipeline import classify as stub
from PIL import Image as PILImage

from . import factories as f
from .assist_support import review_tasks, seed_templates, set_switch, suggestion
from .conftest import LOCAL_HOSTS
from .images_support import FakeStorage, image_row, images_db, run_image_job, use_storage
from .jobs_support import JobWorld, job_row

__all__ = ["images_db"]
pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
CATEGORIES = ("shirts", "t_shirts")


@pytest.fixture
def store(images_db: JobWorld, monkeypatch: pytest.MonkeyPatch) -> FakeStorage:
    import app.workers.category_suggestion as worker

    fake = FakeStorage()
    use_storage(monkeypatch, fake)
    monkeypatch.setattr(worker, "get_storage", lambda: fake)
    seed_templates(images_db.conn, *CATEGORIES)
    set_switch(monkeypatch, "category", on=True)
    stub.LAST_REQUEST = None
    return fake


def _jpeg() -> bytes:
    out = io.BytesIO()
    PILImage.new("RGB", (32, 32), (200, 40, 40)).save(out, format="JPEG")
    return out.getvalue()


def photo(
    conn: Any, store: FakeStorage, tenant: uuid.UUID, product_id: uuid.UUID, *, clean: bool = True
) -> uuid.UUID:
    """A live main/secondary photo; with `clean`, its clean hash, vision size and `vision.jpg`."""
    from app.services.image_keys import derived_key

    extra: dict[str, Any] = {}
    if clean:
        extra = {
            "clean_content_hash": hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
            "clean_mime_type": "image/jpeg",
            "metadata": {"vision": {"w": 32, "h": 32}},
        }
    taken = conn.execute(
        "SELECT count(*) FROM images WHERE product_id = %s AND slot = 'main'", [product_id]
    ).fetchone()[0]
    slot = "secondary" if taken else "main"
    image_id = image_row(conn, tenant, product_id, slot=slot, **extra)
    if clean:
        store.objects[derived_key(tenant, product_id, image_id, "vision.jpg")] = (
            _jpeg(),
            "image/jpeg",
            datetime.now(UTC),
        )
    return image_id


async def enqueue(tenant: uuid.UUID, product_id: uuid.UUID) -> uuid.UUID:
    from app.repositories.uow import tenant_uow
    from app.services.category_suggest import enqueue_suggestion

    async with tenant_uow(tenant) as uow:
        user = await uow.session.execute(
            __import__("sqlalchemy").text(
                "SELECT user_id FROM tenant_memberships WHERE tenant_id = :t LIMIT 1"
            ),
            {"t": tenant},
        )
        product = await uow.products.get_by_id(product_id)
        assert product is not None
        return await enqueue_suggestion(
            uow, tenant, user.scalar_one(), product_id, "0" * 64, category=product.category
        )


async def _digest(tenant: uuid.UUID, product_id: uuid.UUID) -> str:
    from app.core.config import get_settings
    from app.repositories.uow import tenant_uow
    from app.services.category_suggest import suggest_inputs

    async with tenant_uow(tenant) as uow:
        product = await uow.products.get_by_id(product_id)
        assert product is not None
        return (await suggest_inputs(uow, product, get_settings())).digest


def _settled(conn: Any, product_id: uuid.UUID, status: str, digest: str) -> None:
    from psycopg.types.json import Jsonb

    conn.execute(
        "UPDATE products SET category_suggestion = %s WHERE id = %s",
        [
            Jsonb(
                {
                    "status": status,
                    "candidates": [],
                    "input_digest": digest,
                    "calibration_version": "v1",
                    "job_id": None,
                    "at": datetime.now(UTC).isoformat(),
                }
            ),
            product_id,
        ],
    )


def _product(jw: JobWorld, title: str = "Cotton crew tee", **cols: Any) -> uuid.UUID:
    return f.product(jw.conn, jw.tenant("acme"), title=title, **cols)


def _runs(conn: Any, job: uuid.UUID) -> list[dict[str, Any]]:
    cur = conn.execute("SELECT * FROM agent_runs WHERE job_id = %s", [job])
    names = [d.name for d in cur.description or ()]
    return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]


def _reserved(conn: Any, job: uuid.UUID) -> Decimal:
    return Decimal(job_row(conn, job)["cost_reserved_usd"])


def _snapshot(conn: Any, product_id: uuid.UUID) -> Any:
    return conn.execute(
        "SELECT category, attributes, provenance FROM products WHERE id = %s", [product_id]
    ).fetchone()


async def _run(jw: JobWorld, product_id: uuid.UUID, max_runs: int = 1) -> uuid.UUID:
    job = await enqueue(jw.tenant("acme"), product_id)
    await run_image_job(job, max_runs=max_runs)
    return job


async def test_pipeline_without_the_entry_is_unavailable(
    images_db: JobWorld, store: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delattr(mkos_pipeline, "classify_product")
    p = _product(images_db)
    job = await _run(images_db, p)
    assert job_row(images_db.conn, job)["status"] == "succeeded"
    assert suggestion(images_db.conn, p)["status"] == "unavailable"  # type: ignore[index]
    assert _runs(images_db.conn, job) == [] and _reserved(images_db.conn, job) == 0


async def test_switch_off_after_the_ask_is_unavailable_with_no_call(
    images_db: JobWorld, store: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _product(images_db)
    set_switch(monkeypatch, "category", on=False)
    job = await _run(images_db, p)
    assert suggestion(images_db.conn, p)["status"] == "unavailable"  # type: ignore[index]
    assert stub.LAST_REQUEST is None and _runs(images_db.conn, job) == []


async def test_happy_path_stores_suggested_with_its_run(
    images_db: JobWorld, store: FakeStorage
) -> None:
    tenant = images_db.tenant("acme")
    p = _product(images_db, category=None, provenance={"title": f.fact()})
    image = photo(images_db.conn, store, tenant, p)
    before = _snapshot(images_db.conn, p)
    job = await _run(images_db, p)
    assert job_row(images_db.conn, job)["status"] == "succeeded"
    stored = suggestion(images_db.conn, p)
    assert stored is not None and stored["status"] == "suggested"
    assert [c["category"] for c in stored["candidates"]] == ["shirts"]
    assert stored["candidates"][0]["evidence_refs"] == ["text.title", f"images[{image}]"]
    assert stored["job_id"] == str(job) and len(stored["input_digest"]) == 64
    (run,) = _runs(images_db.conn, job)
    assert (run["agent_name"], run["trigger"]) == ("attribute", "vision")
    assert run["product_id"] == p and run["listing_id"] is None
    assert review_tasks(images_db.conn, p) == 0
    assert _snapshot(images_db.conn, p) == before  # never the category, facts or provenance
    assert _reserved(images_db.conn, job) == 0


async def test_request_holds_only_the_product_text(images_db: JobWorld, store: FakeStorage) -> None:
    p = _product(images_db, description="Soft cotton.", brand_name=None)
    await _run(images_db, p)
    request = stub.LAST_REQUEST
    assert request is not None
    assert set(request.text.model_dump()) == {
        "title",
        "description",
        "brand",
        "seller_category",
        "attributes",
    }
    assert request.images == [] and [c.category for c in request.categories] == list(CATEGORIES)
    sent = request.model_dump_json()
    assert "party" not in sent and "gstin" not in sent.lower() and "phone" not in sent


@pytest.mark.parametrize("marker", ["[lowconf]", "[none]", "[outside]"])
async def test_unsure_answers_open_one_review(
    images_db: JobWorld, store: FakeStorage, marker: str
) -> None:
    p = _product(images_db, f"Tee {marker}", category=None, provenance={"title": f.fact()})
    await _run(images_db, p)
    stored = suggestion(images_db.conn, p)
    assert stored is not None and stored["status"] == "suggested"
    if marker == "[outside]":
        assert stored["candidates"] == []  # the unoffered category is dropped
    assert review_tasks(images_db.conn, p, "open") == 1
    await _run(images_db, p)  # a second classification opens no second review
    assert review_tasks(images_db.conn, p) == 1


async def test_a_product_with_a_category_gets_no_review(
    images_db: JobWorld, store: FakeStorage
) -> None:
    p = _product(images_db, "Tee [lowconf]")
    await _run(images_db, p)
    assert suggestion(images_db.conn, p)["status"] == "suggested"  # type: ignore[index]
    assert review_tasks(images_db.conn, p) == 0


async def test_untrusted_record_fails_and_keeps_nothing(
    images_db: JobWorld, store: FakeStorage
) -> None:
    p = _product(images_db, "Tee [badrecord]")
    job = await _run(images_db, p)
    assert job_row(images_db.conn, job)["status"] in {"failed", "dead_letter"}
    assert _runs(images_db.conn, job) == []
    assert suggestion(images_db.conn, p)["status"] == "failed"  # type: ignore[index]


async def test_boom_retries_three_times_then_dead_letters(
    images_db: JobWorld, store: FakeStorage
) -> None:
    p = _product(images_db, "Tee [boom]")
    job = await enqueue(images_db.tenant("acme"), p)
    runs = await run_image_job(job, max_runs=5)
    assert runs == 3 and job_row(images_db.conn, job)["status"] == "dead_letter"
    stored = suggestion(images_db.conn, p)
    assert stored is not None and stored["status"] == "failed" and stored["job_id"] == str(job)


async def test_runs_kept_when_the_suggestion_store_fails(
    images_db: JobWorld, store: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.repositories.products import ProductRepository

    async def broken(self: Any, product_id: uuid.UUID, value: Any) -> bool:
        raise RuntimeError("store failed")

    p = _product(images_db)
    monkeypatch.setattr(ProductRepository, "write_category_suggestion", broken)
    job = await _run(images_db, p)
    assert len(_runs(images_db.conn, job)) == 1  # invariant 7: the run committed first
    assert suggestion(images_db.conn, p) is None


async def test_edit_during_the_call_is_stale(
    images_db: JobWorld, store: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.workers import classify_seam

    p = _product(images_db, category=None, provenance={"title": f.fact()})

    async def edit_then_answer(request: Any, credentials: Any) -> Any:
        images_db.conn.execute("UPDATE products SET title = 'Edited meanwhile' WHERE id = %s", [p])
        images_db.conn.commit()
        return await stub.classify_product(request, credentials)

    monkeypatch.setattr(classify_seam, "call_classify_product", edit_then_answer)
    await _run(images_db, p)
    stored = suggestion(images_db.conn, p)
    assert stored is not None and stored["status"] == "failed"
    assert review_tasks(images_db.conn, p) == 0


async def test_lamps_offered_only_while_switched_on(
    images_db: JobWorld, store: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_templates(images_db.conn, "home_decor_lamps")
    await _run(images_db, _product(images_db))
    assert stub.LAST_REQUEST is not None
    assert "home_decor_lamps" not in [c.category for c in stub.LAST_REQUEST.categories]
    set_switch(monkeypatch, "lamps", on=True)
    await _run(images_db, _product(images_db))
    assert "home_decor_lamps" in [c.category for c in stub.LAST_REQUEST.categories]


async def test_at_most_six_clean_photos_are_sent(images_db: JobWorld, store: FakeStorage) -> None:
    tenant = images_db.tenant("acme")
    p = _product(images_db)
    for _ in range(8):
        photo(images_db.conn, store, tenant, p)
    await _run(images_db, p)
    assert stub.LAST_REQUEST is not None and len(stub.LAST_REQUEST.images) == 6
    q = _product(images_db)
    photo(images_db.conn, store, tenant, q, clean=False)
    await _run(images_db, q)
    assert stub.LAST_REQUEST.images == []


async def test_rationale_control_and_bidi_characters_are_stripped(
    images_db: JobWorld, store: FakeStorage
) -> None:
    p = _product(images_db, "Tee [bidi]")
    await _run(images_db, p)
    stored = suggestion(images_db.conn, p)
    assert stored is not None
    rationale = stored["candidates"][0]["rationale"]
    assert "\x00" not in rationale and "‮" not in rationale and rationale


async def test_staff_requeue_refused(images_db: JobWorld, store: FakeStorage) -> None:
    from app.db.session import unit_of_work
    from app.repositories.jobs import JobRunRepository, NotEligibleError
    from app.services.job_hooks import run_requeue_hook

    tenant = images_db.tenant("acme")
    p = _product(images_db, "Tee [boom]")
    job = await enqueue(tenant, p)
    await run_image_job(job, max_runs=5)
    with pytest.raises(NotEligibleError):
        async with unit_of_work(tenant) as session:
            old, new = await JobRunRepository(session, tenant).requeue(job)
            await run_requeue_hook(session, old, new)
    await asyncio.sleep(0)


async def test_a_category_written_before_the_run_settles_the_suggestion(
    images_db: JobWorld, store: FakeStorage
) -> None:
    p = _product(images_db, category=None)
    job = await enqueue(images_db.tenant("acme"), p)
    images_db.conn.execute("UPDATE products SET category = 'shirts' WHERE id = %s", [p])
    await run_image_job(job)
    stored = suggestion(images_db.conn, p)
    assert stored is not None and stored["status"] == "confirmed"
    assert [c["category"] for c in stored["candidates"]] == ["shirts"]
    assert review_tasks(images_db.conn, p) == 0


async def test_another_category_written_before_the_run_is_overridden(
    images_db: JobWorld, store: FakeStorage
) -> None:
    p = _product(images_db, "Tee [lowconf]", category=None)
    job = await enqueue(images_db.tenant("acme"), p)
    images_db.conn.execute("UPDATE products SET category = 't_shirts' WHERE id = %s", [p])
    await run_image_job(job)
    stored = suggestion(images_db.conn, p)
    assert stored is not None and stored["status"] == "overridden"
    assert review_tasks(images_db.conn, p) == 0


@pytest.mark.parametrize("status", ["confirmed", "overridden"])
async def test_a_settled_suggestion_is_never_overwritten(
    images_db: JobWorld, store: FakeStorage, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    tenant = images_db.tenant("acme")
    p = _product(images_db)
    digest = await _digest(tenant, p)
    _settled(images_db.conn, p, status, digest)
    await _run(images_db, p)  # the same inputs: the seller already decided
    kept = suggestion(images_db.conn, p)
    assert kept is not None and kept["status"] == status and kept["job_id"] is None
    set_switch(monkeypatch, "category", on=False)
    await _run(images_db, p)  # `unavailable` never replaces the seller's decision
    assert suggestion(images_db.conn, p)["status"] == status  # type: ignore[index]
    images_db.conn.execute("UPDATE products SET title = 'Tee [badrecord]' WHERE id = %s", [p])
    set_switch(monkeypatch, "category", on=True)
    await _run(images_db, p)  # nor does a failure
    assert suggestion(images_db.conn, p)["status"] == status  # type: ignore[index]
