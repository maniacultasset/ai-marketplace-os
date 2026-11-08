"""SI12 T6 (AC1-AC4, AC8): `ImageRepository.clear_role`, `live_size_chart`, `size_chart_of` and
`write_size_chart_read` as app_rw."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.repositories.images import NewImage
from app.repositories.uow import tenant_uow
from app.services.image_keys import image_object_key
from mkos_schemas.enums import ImageSlot

from . import factories as f
from .conftest import LOCAL_HOSTS
from .images_support import image_db_row, image_row, images_db, product
from .jobs_support import JobWorld

__all__ = ["images_db"]
pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

HASH = "a" * 64
T0 = datetime(2026, 11, 1, tzinfo=UTC)


def _role(conn: Any, image_id: uuid.UUID) -> str | None:
    role: str | None = image_db_row(conn, image_id)["role"]
    return role


async def test_create_stores_the_role(images_db: JobWorld) -> None:
    acme = images_db.tenant("acme")
    p = product(images_db.conn, acme)
    image_id = uuid.uuid4()
    async with tenant_uow(acme) as uow:
        made = await uow.images.create(
            NewImage(
                id=image_id,
                product_id=p,
                colourway_id=None,
                storage_key=image_object_key(acme, p, image_id),
                slot=ImageSlot.SECONDARY,
                bytes=10,
                position=1,
                role="close_up",
            )
        )
        assert made is not None and made.role == "close_up"
        details = await uow.images.details_for_product(p)
        rows = await uow.images.listing_set(p)
    assert [d.role for d in details] == ["close_up"]
    assert [r.role for r in rows] == ["close_up"]


async def test_clear_role_only_same_role_product_and_colourway(images_db: JobWorld) -> None:
    conn, acme = images_db.conn, images_db.tenant("acme")
    style = product(conn, acme, "style")
    other = product(conn, acme, "style")
    red = f.colourway(conn, acme, style)
    back = image_row(conn, acme, style, role="back")
    close = image_row(conn, acme, style, role="close_up")
    red_back = image_row(conn, acme, style, role="back", colourway_id=red)
    other_back = image_row(conn, acme, other, role="back")
    gone = image_row(conn, acme, style, role="back", deleted_at=T0, key=None)
    async with tenant_uow(acme) as uow:
        cleared = await uow.images.clear_role(style, None, "back")
        again = await uow.images.clear_role(style, None, "back")
    assert cleared == [back]
    assert again == []
    assert _role(conn, back) is None
    assert image_db_row(conn, back)["deleted_at"] is None
    assert image_db_row(conn, back)["slot"] == "secondary"
    assert _role(conn, close) == "close_up"
    assert _role(conn, red_back) == "back"
    assert _role(conn, other_back) == "back"
    assert _role(conn, gone) == "back"  # a deleted row is never touched
    async with tenant_uow(acme) as uow:
        assert await uow.images.clear_role(style, red, "back") == [red_back]


async def test_clear_role_never_reaches_another_tenant(images_db: JobWorld) -> None:
    conn = images_db.conn
    acme, rival = images_db.tenant("acme"), images_db.tenant("bharat")
    p = product(conn, acme)
    back = image_row(conn, acme, p, role="back")
    async with tenant_uow(rival) as uow:
        assert await uow.images.clear_role(p, None, "back") == []
    assert _role(conn, back) == "back"


async def test_live_size_chart_picks_the_newest_style_chart(images_db: JobWorld) -> None:
    conn, acme = images_db.conn, images_db.tenant("acme")
    style = product(conn, acme, "style")
    red = f.colourway(conn, acme, style)
    image_row(conn, acme, style, slot="size_chart", created_at=T0)
    newest = image_row(conn, acme, style, slot="size_chart", created_at=T0 + timedelta(hours=1))
    image_row(
        conn, acme, style, slot="size_chart", created_at=T0 + timedelta(hours=2), deleted_at=T0
    )
    image_row(
        conn, acme, style, slot="size_chart", colourway_id=red, created_at=T0 + timedelta(hours=3)
    )
    image_row(conn, acme, style, slot="main", created_at=T0 + timedelta(hours=4))
    async with tenant_uow(acme) as uow:
        found = await uow.images.live_size_chart(style)
        none = await uow.images.live_size_chart(product(conn, acme))
    assert found is not None and found.id == newest
    assert none is None
    async with tenant_uow(images_db.tenant("bharat")) as uow:
        assert await uow.images.live_size_chart(style) is None


async def test_size_chart_of(images_db: JobWorld) -> None:
    conn, acme = images_db.conn, images_db.tenant("acme")
    p, q = product(conn, acme), product(conn, acme)
    chart = image_row(conn, acme, p, slot="size_chart")
    main = image_row(conn, acme, p, slot="main")
    deleted = image_row(conn, acme, p, slot="size_chart", deleted_at=T0)
    async with tenant_uow(acme) as uow:
        assert (await uow.images.size_chart_of(p, chart)) is not None
        assert await uow.images.size_chart_of(q, chart) is None
        assert await uow.images.size_chart_of(p, main) is None
        assert await uow.images.size_chart_of(p, deleted) is None
        assert await uow.images.size_chart_of(p, uuid.uuid4()) is None
    async with tenant_uow(images_db.tenant("bharat")) as uow:
        assert await uow.images.size_chart_of(p, chart) is None


async def test_write_size_chart_read_fences(images_db: JobWorld) -> None:
    conn, acme = images_db.conn, images_db.tenant("acme")
    p = product(conn, acme)
    chart = image_row(
        conn,
        acme,
        p,
        slot="size_chart",
        clean_content_hash=HASH,
        clean_mime_type="image/jpeg",
        metadata={"vision": {"w": 8, "h": 8}},
    )
    main = image_row(
        conn, acme, p, slot="main", clean_content_hash=HASH, clean_mime_type="image/jpeg"
    )
    deleted = image_row(
        conn,
        acme,
        p,
        slot="size_chart",
        clean_content_hash=HASH,
        clean_mime_type="image/jpeg",
        deleted_at=T0,
    )
    job, newer = uuid.uuid4(), uuid.uuid4()
    value = {"status": "read", "chart": {"unit": "cm"}, "read_at": T0.isoformat()}
    async with tenant_uow(acme) as uow:
        assert not await uow.images.write_size_chart_read(
            chart, value, source_hash="b" * 64, job_id=job
        )
        assert not await uow.images.write_size_chart_read(main, value, source_hash=HASH, job_id=job)
        assert not await uow.images.write_size_chart_read(
            deleted, value, source_hash=HASH, job_id=job
        )
        assert await uow.images.write_size_chart_read(chart, value, source_hash=HASH, job_id=job)
    stored = image_db_row(conn, chart)["metadata"]
    assert stored["vision"] == {"w": 8, "h": 8}  # the other keys stay
    assert stored["size_chart_read"] == {**value, "source_hash": HASH, "job_id": str(job)}
    async with tenant_uow(acme) as uow:
        # a settled result of another job for the same hash is never overwritten
        failed = {"status": "failed", "read_at": T0.isoformat()}
        assert not await uow.images.write_size_chart_read(
            chart, failed, source_hash=HASH, job_id=newer
        )
        # the same job may rewrite its own result
        assert await uow.images.write_size_chart_read(chart, failed, source_hash=HASH, job_id=job)
        # and a failed result gives way to a new job
        assert await uow.images.write_size_chart_read(chart, value, source_hash=HASH, job_id=newer)
    assert image_db_row(conn, chart)["metadata"]["size_chart_read"]["job_id"] == str(newer)


async def test_write_size_chart_read_never_reaches_another_tenant(images_db: JobWorld) -> None:
    conn, acme = images_db.conn, images_db.tenant("acme")
    p = product(conn, acme)
    chart = image_row(
        conn, acme, p, slot="size_chart", clean_content_hash=HASH, clean_mime_type="image/jpeg"
    )
    async with tenant_uow(images_db.tenant("bharat")) as uow:
        written = await uow.images.write_size_chart_read(
            chart, {"status": "read"}, source_hash=HASH, job_id=uuid.uuid4()
        )
    assert not written
    assert "size_chart_read" not in image_db_row(conn, chart)["metadata"]
