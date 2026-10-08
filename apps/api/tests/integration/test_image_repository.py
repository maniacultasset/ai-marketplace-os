"""S17b T4 (AC7, AC11, AC12; DATABASE.md:479-488): the `ImageRepository` additions as app_rw."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from app.repositories.images import ImageMeta, NewImage, rollup
from app.repositories.uow import tenant_uow
from app.services.image_keys import image_object_key
from hypothesis import given
from hypothesis import strategies as st
from mkos_schemas.enums import ImageSlot, Marketplace
from mkos_schemas.images import ImageCheck, ImageReport

from . import factories as f
from .conftest import LOCAL_HOSTS
from .images_support import image_db_row, image_row, images_db, product
from .jobs_support import JobWorld
from .products_support import statements

__all__ = ["images_db"]
pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)


def report(image_id: uuid.UUID, market: str = "amazon", passed: bool = True) -> ImageReport:
    return ImageReport(
        image_id=image_id,
        marketplace=Marketplace(market),
        slot=ImageSlot.MAIN,
        pass_=passed,
        checks=[ImageCheck(check="format", method="deterministic", passed=passed)],
        issues=[],
        processed_at=datetime(2026, 10, 7, tzinfo=UTC),
    )


def new(
    product_id: uuid.UUID, tenant: uuid.UUID, slot: ImageSlot = ImageSlot.MAIN, **kw: Any
) -> NewImage:
    image_id = kw.pop("id", uuid.uuid4())
    return NewImage(
        id=image_id,
        product_id=product_id,
        colourway_id=kw.pop("colourway_id", None),
        storage_key=kw.pop("storage_key", image_object_key(tenant, product_id, image_id)),
        slot=slot,
        bytes=kw.pop("bytes", 10),
        position=kw.pop("position", 0),
    )


async def test_create_and_get_live(images_db: JobWorld) -> None:
    acme = images_db.tenant("acme")
    p = product(images_db.conn, acme)
    async with tenant_uow(acme) as uow:
        row = await uow.images.create(new(p, acme))
        assert row is not None
    async with tenant_uow(acme) as uow:
        found = await uow.images.get_live(row.id)
    assert found is not None and found.status == "uploaded" and found.compliance_status == "pending"
    assert found.bytes == 10


async def test_get_by_key_includes_deleted(images_db: JobWorld) -> None:
    acme = images_db.tenant("acme")
    p = product(images_db.conn, acme)
    image_id = image_row(images_db.conn, acme, p, deleted_at=datetime.now(UTC))
    key = image_db_row(images_db.conn, image_id)["storage_key"]
    async with tenant_uow(acme) as uow:
        assert (await uow.images.get_by_key(key)) is not None
        assert (await uow.images.get_live(image_id)) is None


async def test_unique_storage_key_conflict_returns_none(images_db: JobWorld) -> None:
    acme = images_db.tenant("acme")
    p = product(images_db.conn, acme)
    one = new(p, acme, ImageSlot.SECONDARY)
    async with tenant_uow(acme) as uow:
        assert await uow.images.create(one) is not None
        twin = new(p, acme, ImageSlot.SECONDARY, storage_key=one.storage_key)
        assert await uow.images.create(twin) is None


async def test_next_position_per_slot_and_colourway(images_db: JobWorld) -> None:
    conn, acme = images_db.conn, images_db.tenant("acme")
    style = product(conn, acme, "style")
    cw = f.colourway(conn, acme, style)
    image_row(conn, acme, style, slot="secondary", position=0)
    image_row(conn, acme, style, slot="secondary", position=3)
    image_row(conn, acme, style, slot="secondary", colourway_id=cw, position=0)
    image_row(conn, acme, style, slot="secondary", position=9, deleted_at=datetime.now(UTC))
    async with tenant_uow(acme) as uow:
        assert await uow.images.next_position(style, None, ImageSlot.SECONDARY) == 4
        assert await uow.images.next_position(style, cw, ImageSlot.SECONDARY) == 1
        assert await uow.images.next_position(style, None, ImageSlot.LIFESTYLE) == 0


async def test_live_main_ignores_deleted(images_db: JobWorld) -> None:
    conn, acme = images_db.conn, images_db.tenant("acme")
    p = product(conn, acme)
    image_row(conn, acme, p, slot="main", deleted_at=datetime.now(UTC))
    async with tenant_uow(acme) as uow:
        assert await uow.images.live_main(p, None) is None
    live = image_row(conn, acme, p, slot="main")
    async with tenant_uow(acme) as uow:
        found = await uow.images.live_main(p, None)
    assert found is not None and found.id == live


async def test_soft_delete_then_get_live_none(images_db: JobWorld) -> None:
    conn, acme = images_db.conn, images_db.tenant("acme")
    image_id = image_row(conn, acme, product(conn, acme))
    async with tenant_uow(acme) as uow:
        assert await uow.images.soft_delete(image_id) is True
        assert await uow.images.soft_delete(image_id) is False
    async with tenant_uow(acme) as uow:
        assert await uow.images.get_live(image_id) is None
    assert image_db_row(conn, image_id)["deleted_at"] is not None


async def test_write_report_one_statement(images_db: JobWorld) -> None:
    conn, acme = images_db.conn, images_db.tenant("acme")
    image_id = image_row(conn, acme, product(conn, acme))
    reports = {
        Marketplace.AMAZON: report(image_id),
        Marketplace.FLIPKART: report(image_id, "flipkart", False),
    }
    meta = ImageMeta(mime_type="image/jpeg", width_px=800, height_px=600)
    async with tenant_uow(acme) as uow:
        with statements() as seen:
            status = await uow.images.write_report(
                image_id, reports=reports, meta=meta, content_hash="ab" * 32
            )
    updates = [s for s in seen if s.lstrip().upper().startswith("UPDATE")]
    assert len(updates) == 1
    assert status == "failed"
    row = image_db_row(conn, image_id)
    assert set(row["image_report_json"]) == {"amazon", "flipkart"}
    assert (
        "pass" in row["image_report_json"]["amazon"]
        and "pass_" not in row["image_report_json"]["amazon"]
    )
    assert (row["compliance_status"], row["mime_type"], row["width_px"], row["height_px"]) == (
        "failed",
        "image/jpeg",
        800,
        600,
    )
    assert row["content_hash"] == "ab" * 32


async def test_write_report_check_constraint_rejects_unknown_key(images_db: JobWorld) -> None:
    conn, acme = images_db.conn, images_db.tenant("acme")
    image_id = image_row(conn, acme, product(conn, acme))
    with pytest.raises(Exception) as caught:
        conn.execute(
            "UPDATE images SET image_report_json = '{\"etsy\": {}}'::jsonb WHERE id = %s",
            [image_id],
        )
    assert "ck_images_image_report_json" in str(caught.value)


async def test_live_keys_order_cp7(images_db: JobWorld) -> None:
    conn, acme = images_db.conn, images_db.tenant("acme")
    p = product(conn, acme)
    ids = {
        "pack": image_row(conn, acme, p, slot="packaging_label"),
        "life": image_row(conn, acme, p, slot="lifestyle"),
        "sec1": image_row(conn, acme, p, slot="secondary", position=1),
        "sec0": image_row(conn, acme, p, slot="secondary", position=0),
        "main": image_row(conn, acme, p, slot="main"),
    }
    key = {n: image_db_row(conn, i)["storage_key"] for n, i in ids.items()}
    async with tenant_uow(acme) as uow:
        found = await uow.images.live_keys_for_products([p])
    assert found == {p: [key["main"], key["sec0"], key["sec1"], key["life"], key["pack"]]}


async def test_live_keys_skip_deleted(images_db: JobWorld) -> None:
    conn, acme = images_db.conn, images_db.tenant("acme")
    p, q = product(conn, acme), product(conn, acme)
    image_row(conn, acme, p, deleted_at=datetime.now(UTC))
    async with tenant_uow(acme) as uow:
        assert await uow.images.live_keys_for_products([p, q]) == {p: [], q: []}


async def test_registered_keys_counts_deleted(images_db: JobWorld) -> None:
    conn, acme = images_db.conn, images_db.tenant("acme")
    p = product(conn, acme)
    live = image_db_row(conn, image_row(conn, acme, p))["storage_key"]
    gone = image_db_row(conn, image_row(conn, acme, p, deleted_at=datetime.now(UTC)))["storage_key"]
    async with tenant_uow(acme) as uow:
        assert await uow.images.registered_keys([live, gone, f"{acme}/x"]) == {live, gone}


@pytest.mark.parametrize(
    "read",
    [
        "get_live",
        "get_by_key",
        "count_live",
        "registered_keys",
        "live_keys_for_products",
        "live_reports_for_images",
    ],
)
async def test_other_tenant_rows_invisible(images_db: JobWorld, read: str) -> None:
    conn, acme, bharat = images_db.conn, images_db.tenant("acme"), images_db.tenant("bharat")
    p = product(conn, bharat)
    image_id = image_row(conn, bharat, p, slot="main")
    key = image_db_row(conn, image_id)["storage_key"]
    async with tenant_uow(acme) as uow:
        repo = uow.images
        calls: dict[str, Any] = {
            "get_live": lambda: repo.get_live(image_id),
            "get_by_key": lambda: repo.get_by_key(key),
            "count_live": lambda: repo.count_live(p),
            "registered_keys": lambda: repo.registered_keys([key]),
            "live_keys_for_products": lambda: repo.live_keys_for_products([p]),
            "live_reports_for_images": lambda: repo.live_reports_for_images([image_id]),
        }
        found = await calls[read]()
    assert found in (None, 0, set(), {p: []}, {})


async def test_lock_serialises_two_sessions(images_db: JobWorld) -> None:
    conn, acme = images_db.conn, images_db.tenant("acme")
    p = product(conn, acme)
    order: list[str] = []
    held = asyncio.Event()

    async def first() -> None:
        async with tenant_uow(acme) as uow:
            await uow.images.lock_product_images(p)
            held.set()
            await asyncio.sleep(0.5)
            order.append("first-commit")

    async def second() -> None:
        await held.wait()
        async with tenant_uow(acme) as uow:
            await uow.images.lock_product_images(p)
            order.append("second-locked")

    await asyncio.gather(first(), second())
    assert order == ["first-commit", "second-locked"]


async def test_latest_for_entity_newest(images_db: JobWorld) -> None:
    from .jobs_support import insert_job

    conn, acme = images_db.conn, images_db.tenant("acme")
    entity = uuid.uuid4()
    insert_job(
        conn,
        acme,
        kind="image_compliance",
        entity_type="image",
        entity_id=entity,
        queued_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    newest = insert_job(
        conn,
        acme,
        kind="image_compliance",
        entity_type="image",
        entity_id=entity,
        queued_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    async with tenant_uow(acme) as uow:
        found = await uow.job_runs.latest_for_entity("image", entity)
        none = await uow.job_runs.latest_for_entity("image", uuid.uuid4())
    assert found is not None and found.id == newest
    assert none is None


async def test_live_reports_for_images(images_db: JobWorld) -> None:
    conn, acme = images_db.conn, images_db.tenant("acme")
    p = product(conn, acme)
    pending = image_row(conn, acme, p)
    checked = image_row(conn, acme, p, slot="main")
    gone = image_row(conn, acme, p, deleted_at=datetime.now(UTC))
    async with tenant_uow(acme) as uow:
        await uow.images.write_report(
            checked, reports={Marketplace.AMAZON: report(checked)}, meta=None, content_hash="00"
        )
    async with tenant_uow(acme) as uow:
        with statements() as seen:
            found = await uow.images.live_reports_for_images([pending, checked, gone, uuid.uuid4()])
    assert len([s for s in seen if "FROM images" in s]) == 1
    assert set(found) == {pending, checked}
    assert found[pending] == {}
    assert found[checked][Marketplace.AMAZON].pass_ is True


@given(st.lists(st.booleans(), max_size=2))
def test_rollup_worst_of(passes: list[bool]) -> None:
    markets = [Marketplace.AMAZON, Marketplace.FLIPKART]
    reports = {markets[i]: report(uuid.uuid4(), markets[i].value, p) for i, p in enumerate(passes)}
    expected = "pending" if not passes else ("passed" if all(passes) else "failed")
    assert rollup(reports) == expected
