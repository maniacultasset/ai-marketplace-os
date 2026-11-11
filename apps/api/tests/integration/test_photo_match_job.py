"""V1.1 (client feedback item 9): the variant photo match, as app_rw through the real wrapper and the
stub pipeline. A product photo that passed its checks queues one check per image content and
colour; the verdict lands on the image; a mismatch or an unsure photo asks the seller; the
seller's keep is recorded; an unresolved mismatch blocks approval and an unsure one warns."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import pytest
from mkos_schemas.codes import ErrorCode
from psycopg.types.json import Jsonb

from .conftest import LOCAL_HOSTS
from .photo_support import OPERATOR, PhotoWorld, approve, build_photo_world, generate

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)


def _switch(monkeypatch: pytest.MonkeyPatch, *, on: bool) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("PHOTO_MATCH_ENABLED", "true" if on else "false")
    get_settings.cache_clear()


@pytest.fixture
async def pw(auth_db: Any, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[PhotoWorld]:
    from app.db.queue import get_queue_app
    from app.workers import photo_match

    get_queue_app.cache_clear()
    world = await build_photo_world(auth_db, monkeypatch)
    monkeypatch.setattr(photo_match, "get_storage", lambda: world.store)
    _switch(monkeypatch, on=True)
    yield world
    get_queue_app.cache_clear()


async def _queue(pw: PhotoWorld, image_id: uuid.UUID) -> uuid.UUID | None:
    from app.repositories.uow import tenant_uow
    from app.services.photo_match import queue_photo_match

    async with tenant_uow(pw.tenant) as uow:
        return await queue_photo_match(uow, image_id)


async def _run(job_id: uuid.UUID) -> None:
    from app.workers import wrapper
    from app.workers.tasks import register_bodies

    register_bodies()
    await wrapper.run_job(str(job_id))


def _stored(pw: PhotoWorld, image_id: uuid.UUID) -> dict[str, Any] | None:
    row = pw.conn.execute("SELECT metadata FROM images WHERE id = %s", [image_id]).fetchone()
    assert row is not None
    found = (row[0] or {}).get("photo_match")
    return found if isinstance(found, dict) else None


def _title(pw: PhotoWorld, title: str) -> None:
    pw.conn.execute("UPDATE products SET title = %s WHERE id = %s", [title, pw.product_id])


def _records(job_id: uuid.UUID, pw: PhotoWorld) -> list[tuple[Any, ...]]:
    rows = pw.conn.execute(
        "SELECT agent_name, trigger, prompt_version FROM agent_runs WHERE job_id = %s",
        [job_id],
    ).fetchall()
    return [tuple(row) for row in rows]


async def _read(pw: PhotoWorld, image_id: uuid.UUID) -> Any:
    from app.services import products

    view = await products.get_product(pw.world.principal(OPERATOR), pw.product_id)
    return next(d.photo_match for d in view.image_details if d.image_id == image_id)


async def _keep(pw: PhotoWorld, image_id: uuid.UUID) -> dict[str, Any]:
    from app.services.photo_match import keep_photo

    return await keep_photo(
        pw.world.principal(OPERATOR),
        str(uuid.uuid4()),
        f"/api/v1/images/{image_id}/photo-match/keep",
        image_id,
    )


async def test_a_secondary_photo_is_checked_once_against_the_main_photo(pw: PhotoWorld) -> None:
    import mkos_pipeline.photo_match as stub

    image = pw.images["secondary"]
    job = await _queue(pw, image)
    assert job is not None
    assert await _queue(pw, image) is None  # the same content and colour: no second check
    assert (await _read(pw, image)).status == "checking"
    await _run(job)
    row = pw.job(job)
    assert row["status"] == "succeeded" and row["entity_type"] == "photo_match"
    assert row["result_json"]["status"] == "match"
    stored = _stored(pw, image)
    assert stored is not None and stored["status"] == "match" and stored["question"] is None
    assert stored["product_match"] == 0.95 and stored["checked_at"]
    sent = stub.LAST_REQUEST[0]
    assert sent.image.image_id == image and sent.reference.image_id == pw.images["main"]
    assert sent.colour == stored["colour"]  # the product's own colour fact
    assert _records(job, pw) == [("image", "vision", "photo_match/stub-1")]
    assert Decimal(row["cost_reserved_usd"]) == 0
    state = await _read(pw, image)
    assert state.status == "match" and not state.kept
    assert await _queue(pw, image) is None  # checked: never paid for again


async def test_the_main_photo_is_checked_by_its_title(pw: PhotoWorld) -> None:
    import mkos_pipeline.photo_match as stub

    job = await _queue(pw, pw.images["main"])
    assert job is not None
    await _run(job)
    assert stub.LAST_REQUEST[0].reference is None and stub.LAST_REQUEST[0].title


async def test_a_mismatch_asks_the_seller_blocks_approval_until_kept(pw: PhotoWorld) -> None:
    accepted = await generate(pw, None)
    _title(pw, "Cotton Kurta [photo-mismatch]")
    jobs = [await _queue(pw, pw.images[slot]) for slot in ("main", "secondary", "lifestyle")]
    for job in jobs:
        assert job is not None
        await _run(job)
    for slot in ("main", "secondary", "lifestyle"):
        state = await _read(pw, pw.images[slot])
        assert state.status == "mismatch" and state.question.startswith("Is this photo the ")
    refused = await approve(pw, accepted.listing_id)
    assert refused.http_status == 422
    assert refused.error is not None and refused.error.error_code is ErrorCode.IMAGE_NON_COMPLIANT
    for slot in ("main", "secondary", "lifestyle"):
        kept = await _keep(pw, pw.images[slot])
        assert kept["photo_match"]["kept"] is True
    audits = pw.conn.execute(
        "SELECT count(*) FROM audit_log WHERE action = 'image.photo_match_kept'"
    ).fetchone()
    assert audits is not None and audits[0] == 3
    assert (await approve(pw, accepted.listing_id)).http_status == 200


async def test_unsure_only_warns(pw: PhotoWorld) -> None:
    accepted = await generate(pw, None)
    _title(pw, "Cotton Kurta [photo-unsure]")
    for slot in ("main", "secondary", "lifestyle"):
        job = await _queue(pw, pw.images[slot])
        assert job is not None
        await _run(job)
        assert (await _read(pw, pw.images[slot])).status == "unsure"
    assert (await approve(pw, accepted.listing_id)).http_status == 200


async def test_a_new_colour_is_a_new_check(pw: PhotoWorld) -> None:
    image = pw.images["secondary"]
    first = await _queue(pw, image)
    assert first is not None
    await _run(first)
    pw.conn.execute(
        "UPDATE products SET attributes = attributes || jsonb_build_object('color', %s::jsonb) "
        "WHERE id = %s",
        [Jsonb({"value": "Mustard Yellow", "source": "seller"}), pw.product_id],
    )
    second = await _queue(pw, image)
    assert second is not None and second != first
    await _run(second)
    stored = _stored(pw, image)
    assert stored is not None and stored["colour"] == "Mustard Yellow"


async def test_only_checked_product_photos_queue(pw: PhotoWorld) -> None:
    assert await _queue(pw, pw.images["packaging_label"]) is None
    pending = pw.add_image("secondary")
    pw.conn.execute("UPDATE images SET compliance_status = 'failed' WHERE id = %s", [pending])
    assert await _queue(pw, pending) is None


async def test_switch_off_queues_nothing_and_a_queued_check_makes_no_call(
    pw: PhotoWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = pw.images["secondary"]
    job = await _queue(pw, image)
    assert job is not None
    _switch(monkeypatch, on=False)
    assert await _queue(pw, pw.images["lifestyle"]) is None
    await _run(job)
    assert pw.job(job)["result_json"]["status"] == "skipped"
    stored = _stored(pw, image)
    assert stored is not None and stored["status"] == "failed" and stored["retry"] is True
    assert _records(job, pw) == []
    # back on, the check that made no call may run again under a key of its own
    _switch(monkeypatch, on=True)
    again = await _queue(pw, image)
    assert again is not None and again != job
    await _run(again)
    assert (_stored(pw, image) or {})["status"] == "match"


async def test_pipeline_without_the_entry_point_is_unavailable(
    pw: PhotoWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mkos_pipeline

    monkeypatch.delattr(mkos_pipeline, "photo_match")
    job = await _queue(pw, pw.images["secondary"])
    assert job is not None
    await _run(job)
    assert pw.job(job)["result_json"]["status"] == "unavailable"
    assert _records(job, pw) == []
    assert (await _read(pw, pw.images["secondary"])).status == "failed"  # not checked


async def test_budget_refusal_makes_no_call(
    pw: PhotoWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.workers import budget

    async def refuse(*_a: Any, **_kw: Any) -> bool:
        raise budget.BudgetRefused("tenant")

    monkeypatch.setattr(budget, "reserve", refuse)
    job = await _queue(pw, pw.images["secondary"])
    assert job is not None
    await _run(job)
    assert pw.job(job)["result_json"]["status"] == "budget"
    assert _records(job, pw) == []


async def test_a_failed_model_call_reads_as_not_checked(pw: PhotoWorld) -> None:
    _title(pw, "Cotton Kurta [photo-failed]")
    job = await _queue(pw, pw.images["secondary"])
    assert job is not None
    await _run(job)
    assert pw.job(job)["result_json"]["status"] == "failed"
    assert (await _read(pw, pw.images["secondary"])).status == "failed"
    assert len(_records(job, pw)) == 1  # invariant 7: the failed call is recorded too


async def test_a_replaced_photo_drops_the_stale_result(pw: PhotoWorld) -> None:
    image = pw.images["secondary"]
    job = await _queue(pw, image)
    assert job is not None
    pw.conn.execute(
        "UPDATE images SET clean_content_hash = %s WHERE id = %s", ["e" * 64, image]
    )  # other pixels before the check ran
    await _run(job)
    assert pw.job(job)["result_json"]["status"] == "superseded"
    assert _records(job, pw) == []  # no call for pixels the key does not name
    assert await _read(pw, image) is None  # nothing made for the old pixels shows
