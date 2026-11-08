"""SI1 T18 (AC10, AC11; decision 19; CP5): the listing order and the context pick."""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.adapters.storage import StorageNotFound
from app.repositories.images import ListingImageRow
from app.services.generation_images import load_context_images, order_for_listing, pick_context
from app.services.image_keys import derived_key
from mkos_schemas.enums import ImageSlot

T0 = datetime(2026, 11, 6, tzinfo=UTC)
PRODUCT = uuid.uuid4()
TENANT = uuid.uuid4()
RED, BLUE = uuid.uuid4(), uuid.uuid4()
_n = iter(range(10_000))


def row(
    slot: str,
    *,
    colourway: uuid.UUID | None = None,
    cw_pos: int | None = None,
    position: int = 0,
    clean: bool = True,
    age: int = 0,
    role: str | None = None,
) -> ListingImageRow:
    return ListingImageRow(
        image_id=uuid.UUID(int=next(_n) + 1),
        product_id=PRODUCT,
        slot=slot,
        colourway_id=colourway,
        position=position,
        clean_content_hash="a" * 64 if clean else None,
        vision={"w": 800, "h": 1000},
        colourway_position=cw_pos,
        created_at=T0 + timedelta(minutes=age),
        role=role,
    )


def test_decision_19_order_with_two_colourways() -> None:
    blue_main = row("main", colourway=BLUE, cw_pos=1)
    red_second = row("secondary", colourway=RED, cw_pos=0, position=1)
    red_main = row("main", colourway=RED, cw_pos=0)
    chart = row("size_chart")
    life = row("lifestyle")
    sec1 = row("secondary", position=1)
    sec0 = row("secondary", position=0)
    main = row("main")
    label = row("packaging_label")
    rows = [blue_main, red_second, red_main, chart, life, sec1, sec0, main, label]
    refs = order_for_listing(rows)
    assert [r.image_id for r in refs] == [
        main.image_id,
        sec0.image_id,
        sec1.image_id,
        life.image_id,
        chart.image_id,
        red_main.image_id,
        blue_main.image_id,
        red_second.image_id,
    ]
    assert [r.position for r in refs] == list(range(8))
    assert ImageSlot.PACKAGING_LABEL not in {r.slot for r in refs}


def test_cut_at_twenty() -> None:
    rows = [row("main")] + [row("secondary", position=i) for i in range(24)]
    refs = order_for_listing(rows)
    assert len(refs) == 20 and refs[0].slot is ImageSlot.MAIN


def test_cap_keeps_every_colourway_main() -> None:
    """Decision 19 (round 1 repro): every colourway's main comes before any colourway's other
    images, so the 20 cap never drops a later colourway's main for an earlier one's extras."""
    rows = [row("main")] + [row("secondary", position=i) for i in range(5)]
    rows += [row("main", colourway=RED, cw_pos=0)]
    rows += [row("secondary", colourway=RED, cw_pos=0, position=i) for i in range(1, 15)]
    blue_main = row("main", colourway=BLUE, cw_pos=1)
    rows.append(blue_main)
    refs = order_for_listing(rows)
    assert len(refs) == 20
    assert [r.image_id for r in refs].index(blue_main.image_id) == 7


def test_context_follows_the_listing_order() -> None:
    red_main, red_sec = row("main", colourway=RED, cw_pos=0), row(
        "secondary", colourway=RED, cw_pos=0, position=1
    )
    blue_main = row("main", colourway=BLUE, cw_pos=1)
    picked = pick_context([red_sec, blue_main, red_main])
    assert [p.image_id for p in picked] == [red_main.image_id, blue_main.image_id, red_sec.image_id]


def test_pending_or_failed_images_still_listed() -> None:
    """CP5: compliance does not filter; the approve gate judges them."""
    assert len(order_for_listing([row("main", clean=False)])) == 1


def test_context_is_six_plus_newest_label_main_first() -> None:
    rows = [row("secondary", position=i) for i in range(8)] + [row("main")]
    old, new = row("packaging_label", age=1), row("packaging_label", age=5)
    picked = pick_context([*rows, old, new])
    assert len(picked) == 7
    assert picked[0].slot == "main"
    assert picked[-1].image_id == new.image_id


def test_context_skips_rows_without_clean_copy() -> None:
    picked = pick_context([row("main", clean=False), row("secondary")])
    assert [p.slot for p in picked] == ["secondary"]


def test_empty_set() -> None:
    assert order_for_listing([]) == [] and pick_context([]) == []


JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


class _Store:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects

    async def get(self, bucket: str, key: str, *, max_bytes: int | None = None) -> bytes:
        if key not in self.objects:
            raise StorageNotFound("missing")
        return self.objects[key]


async def test_load_context_images_skips_missing() -> None:
    present, missing = row("main"), row("secondary")
    key = derived_key(TENANT, PRODUCT, present.image_id, "vision.jpg")
    store: Any = _Store({key: JPEG})
    images = await load_context_images(store, TENANT, [present, missing])
    assert [i.image_id for i in images] == [present.image_id]
    assert base64.b64decode(images[0].data_b64) == JPEG
    assert (images[0].width, images[0].height) == (800, 1000)


@pytest.mark.parametrize("data", [b"not a jpeg", b""])
async def test_load_context_images_skips_bad_bytes(data: bytes) -> None:
    present = row("main")
    key = derived_key(TENANT, PRODUCT, present.image_id, "vision.jpg")
    store: Any = _Store({key: data})
    assert await load_context_images(store, TENANT, [present]) == []


# ---- SI12 T8 (AC3): Back and Close-up sort right after the main ----


def test_roles_order_main_back_close_up_then_the_rest() -> None:
    chart = row("size_chart")
    life = row("lifestyle")
    plain = row("secondary", position=0, age=0)
    close = row("secondary", position=1, age=1, role="close_up")  # uploaded first
    back = row("secondary", position=2, age=2, role="back")
    main = row("main")
    label = row("packaging_label")
    rows = [chart, life, plain, close, back, main, label]
    expected = [main, back, close, plain, life, chart]
    assert [r.image_id for r in order_for_listing(rows)] == [r.image_id for r in expected]
    assert [r.image_id for r in pick_context(rows)] == [
        *(r.image_id for r in expected),
        label.image_id,
    ]


def test_roles_keep_colourway_phases() -> None:
    red_back = row("secondary", colourway=RED, cw_pos=0, position=3, role="back")
    red_plain = row("secondary", colourway=RED, cw_pos=0, position=0)
    red_main = row("main", colourway=RED, cw_pos=0)
    style_plain = row("secondary", position=0)
    style_back = row("secondary", position=5, role="back")
    main = row("main")
    refs = order_for_listing([red_back, red_plain, red_main, style_plain, style_back, main])
    assert [r.image_id for r in refs] == [
        main.image_id,
        style_back.image_id,
        style_plain.image_id,
        red_main.image_id,
        red_back.image_id,
        red_plain.image_id,
    ]


def test_context_keeps_six_plus_label_with_roles() -> None:
    rows = [row("main"), row("secondary", role="back"), row("secondary", role="close_up")]
    rows += [row("secondary", position=i) for i in range(6)]
    label_old, label_new = row("packaging_label", age=1), row("packaging_label", age=9)
    picked = pick_context([*rows, label_old, label_new])
    assert len(picked) == 7
    assert picked[-1].image_id == label_new.image_id
    assert [r.role for r in picked[:3]] == [None, "back", "close_up"]
    assert all(r.slot != "packaging_label" for r in order_for_listing([*rows, label_new]))


async def test_context_image_carries_the_role() -> None:
    back, main = row("secondary", role="back"), row("main")
    objects = {derived_key(TENANT, PRODUCT, r.image_id, "vision.jpg"): JPEG for r in (back, main)}
    store: Any = _Store(objects)
    images = await load_context_images(store, TENANT, [main, back])
    assert [(i.slot, i.role) for i in images] == [
        (ImageSlot.MAIN, None),
        (ImageSlot.SECONDARY, "back"),
    ]
