"""V1.1: which photo queues a read on upload, the read's job key and photo order."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from app.repositories.images import ListingImageRow
from app.services.photo_read_upload import job_key, triggers_read, upload_photo_set
from app.workers.photo_facts_read import ordered_photos
from mkos_schemas.jobs import ContextImage

JPEG = (
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDABALDA4MChAODQ4SERATGCgaGBYWGDEjJR0oOjM9PDkzODdASFxO"
    "QERXRTc4UG1RV19iZ2hnPk1xeXBkeFxlZ2P/wAALCAAIAAgBAREA/8QAFAABAAAAAAAAAAAAAAAAAAAABv/EABQQ"
    "AQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AJv/Z"
)


@pytest.mark.parametrize(
    ("slot", "role", "queues"),
    [
        ("main", None, True),
        ("secondary", "back", True),
        ("secondary", "close_up", True),
        ("secondary", None, False),
        ("lifestyle", None, False),
        ("size_chart", None, False),
        ("packaging_label", None, False),
    ],
)
def test_front_back_and_close_up_queue_a_read(slot: str, role: str | None, queues: bool) -> None:
    assert triggers_read(slot, role) is queues


def _row(slot: str, *, clean: str | None = "h", vision: bool = True, **kw: Any) -> ListingImageRow:
    return ListingImageRow(
        image_id=kw.pop("image_id", uuid.uuid4()),
        product_id=uuid.UUID(int=1),
        slot=slot,
        colourway_id=None,
        position=0,
        clean_content_hash=clean,
        vision={"w": 8, "h": 8} if vision else None,
        colourway_position=None,
        created_at=datetime(2026, 11, 10, tzinfo=UTC),
        **kw,
    )


def test_photo_set_is_product_photos_with_a_vision_copy() -> None:
    main, back = _row("main", clean="a"), _row("secondary", clean="b", role="back")
    rows = [main, back, _row("packaging_label"), _row("lifestyle", clean=None)]
    rows.append(_row("secondary", vision=False))
    assert upload_photo_set(rows) == tuple(
        sorted([(str(main.image_id), "a"), (str(back.image_id), "b")])
    )
    assert upload_photo_set([_row("packaging_label")]) == ()


def test_job_key_follows_the_product_and_the_set() -> None:
    product = uuid.UUID(int=7)
    one = (("i1", "h1"),)
    key = job_key(product, one)
    assert len(key) == 64 and int(key, 16) >= 0
    assert job_key(product, one) == key
    assert job_key(product, (("i1", "h2"),)) != key  # a new clean copy is a new set
    assert job_key(uuid.UUID(int=8), one) != key


def _image(slot: str, role: str | None = None) -> ContextImage:
    return ContextImage(
        image_id=uuid.uuid4(), slot=slot, role=role, width=8, height=8, data_b64=JPEG
    )


def test_photos_go_front_back_close_up_then_the_rest_at_most_four() -> None:
    life, plain = _image("lifestyle"), _image("secondary")
    close, back, front = (
        _image("secondary", "close_up"),
        _image("secondary", "back"),
        _image("main"),
    )
    label = _image("packaging_label")
    assert ordered_photos([life, plain, label, close, back, front]) == [front, back, close, plain]
    assert ordered_photos([label]) == []
