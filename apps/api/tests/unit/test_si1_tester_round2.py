"""SI1 tester round 2: property tests on the round 1 fixes (CP14 money across a family, AC3;
decision 19 listing order, AC11; PNG transparency in derived copies, AC9, AC12). Each property
is checked against a brute-force model of the rule, not against the code's own helper."""

from __future__ import annotations

import io
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from app.core.errors import InputError
from app.repositories.images import ListingImageRow
from app.services.generation_images import MAX_LISTING_IMAGES, order_for_listing
from app.services.image_derive import derive
from app.services.intake_values import inherited_money_breach
from app.services.product_edit import plan_patch
from hypothesis import given, settings
from hypothesis import strategies as st
from mkos_schemas.enums import ImageSlot
from PIL import Image

NOW = datetime(2026, 11, 7, tzinfo=UTC)
Money = tuple[Decimal | None, Decimal | None]

# ---- CP14 across a family (AC3, PATCH leg) ----

_amount = st.one_of(st.none(), st.integers(1, 12).map(Decimal))


def _holds(price: Decimal | None, mrp: Decimal | None) -> bool:
    return price is None or mrp is None or price <= mrp


def _effective(own: Money, style: Money) -> Money:
    return (
        own[0] if own[0] is not None else style[0],
        own[1] if own[1] is not None else style[1],
    )


def _family_holds(style: Money, children: list[Money]) -> bool:
    return _holds(*style) and all(_holds(*_effective(c, style)) for c in children)


@st.composite
def _style_case(draw: Any) -> tuple[Money, list[Money], dict[str, Decimal | None]]:
    """A stored family that holds CP14, and a style money patch."""
    stored = (draw(_amount), draw(_amount))
    children = draw(st.lists(st.tuples(_amount, _amount), max_size=4))
    patch: dict[str, Decimal | None] = {}
    for name in draw(st.sets(st.sampled_from(["selling_price", "mrp"]), min_size=1)):
        patch[name] = draw(_amount)
    return stored, children, patch


@settings(max_examples=400, deadline=None)
@given(_style_case())
def test_style_money_patch_refused_exactly_when_the_family_breaks(
    case: tuple[Money, list[Money], dict[str, Decimal | None]],
) -> None:
    stored, children, patch = case
    if not _family_holds(stored, children):
        return  # the stored family already breaks CP14: not a state the API can reach
    row = SimpleNamespace(
        id=uuid.uuid4(),
        title="Tee",
        category="t_shirts",
        style_number=None,
        selling_price=stored[0],
        mrp=stored[1],
        manufacturer_party_id=None,
        parent_product_id=None,
        variation_theme="size",
        attributes={},
        provenance={},
    )
    new_style = (
        patch.get("selling_price", stored[0]) if "selling_price" in patch else stored[0],
        patch.get("mrp", stored[1]) if "mrp" in patch else stored[1],
    )
    expect_ok = _family_holds(new_style, children)
    try:
        plan_patch(
            row,
            None,
            patch,
            (),
            level="style",
            has_listing=False,
            now=NOW,
            children=children,
        )
    except InputError as exc:
        assert not expect_ok, (stored, children, patch, exc.field)
        assert exc.field in ("selling_price", "mrp")
        return
    assert expect_ok, (stored, children, patch)


# ---- CP14 across a family (AC3, CSV leg helper) ----


@settings(max_examples=400, deadline=None)
@given(st.tuples(_amount, _amount), st.tuples(_amount, _amount))
def test_inherited_breach_matches_the_effective_pair(child: Money, style: Money) -> None:
    side = inherited_money_breach(child, style)
    inherits_one = (child[0] is None) != (child[1] is None)
    if not inherits_one:
        assert side is None  # each row's own CHECK covers it
        return
    breaks = not _holds(*_effective(child, style))
    assert (side is not None) == breaks
    if side is not None:
        assert side == ("selling_price" if child[0] is None else "mrp")


# ---- decision 19 order under the cap (AC11) ----

T0 = datetime(2026, 11, 7, tzinfo=UTC)
_CW = [uuid.UUID(int=20_000 + i) for i in range(5)]
_PUBLISHABLE = [s.value for s in ImageSlot if s is not ImageSlot.PACKAGING_LABEL]


@st.composite
def _rows(draw: Any) -> list[ListingImageRow]:
    rows = []
    for i in range(draw(st.integers(0, 45))):
        cw = draw(st.sampled_from([None, *_CW]))
        rows.append(
            ListingImageRow(
                image_id=uuid.UUID(int=i + 1),
                product_id=uuid.UUID(int=999),
                slot=draw(st.sampled_from(_PUBLISHABLE)),
                colourway_id=cw,
                position=draw(st.integers(0, 30)),
                clean_content_hash="a" * 64,
                vision={"w": 10, "h": 10},
                colourway_position=None if cw is None else _CW.index(cw),
                created_at=T0 + timedelta(seconds=draw(st.integers(0, 50))),
            )
        )
    return rows


def _phase(row: ListingImageRow) -> int:
    if row.colourway_id is None:
        return 0
    return 1 if row.slot == ImageSlot.MAIN.value else 2


@settings(max_examples=300, deadline=None)
@given(_rows())
def test_listing_order_phases_and_cut(rows: list[ListingImageRow]) -> None:
    by_id = {r.image_id: r for r in rows}
    out = [by_id[ref.image_id] for ref in order_for_listing(rows)]
    phases = [_phase(r) for r in out]
    assert phases == sorted(phases)  # style images, then colourway mains, then the rest
    assert len(out) == min(len(rows), MAX_LISTING_IMAGES)
    kept = {r.image_id for r in out}
    for r in rows:
        if r.image_id in kept:
            continue
        # a dropped image never loses its place to an image of a later phase
        assert all(_phase(k) <= _phase(r) for k in out)
    # inside the colourway phases, colourways go by position
    for phase in (1, 2):
        positions = [r.colourway_position or 0 for r in out if _phase(r) == phase]
        assert positions == sorted(positions)


def test_every_colourway_main_survives_when_style_leaves_room() -> None:
    """4 style images, 5 colourways with a main and 6 extras each (39 rows): all 5 mains fit."""
    rows: list[ListingImageRow] = []
    n = 0

    def add(slot: str, cw: uuid.UUID | None, position: int) -> None:
        nonlocal n
        n += 1
        rows.append(
            ListingImageRow(
                image_id=uuid.UUID(int=n),
                product_id=uuid.UUID(int=999),
                slot=slot,
                colourway_id=cw,
                position=position,
                clean_content_hash="a" * 64,
                vision={"w": 10, "h": 10},
                colourway_position=None if cw is None else _CW.index(cw),
                created_at=T0,
            )
        )

    add("main", None, 0)
    for i in range(3):
        add("secondary", None, i)
    for cw in _CW:
        add("main", cw, 0)
        for i in range(6):
            add("secondary", cw, i + 1)
    refs = order_for_listing(rows)
    by_id = {r.image_id: r for r in rows}
    kept = [by_id[ref.image_id] for ref in refs]
    assert len(kept) == 20
    assert {r.colourway_id for r in kept if r.slot == "main" and r.colourway_id} == set(_CW)


# ---- transparency through derive (AC9, AC12) ----


def _png(img: Image.Image, **kw: Any) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", **kw)
    return buf.getvalue()


def _palette() -> Image.Image:
    img = Image.new("P", (48, 48), 0)
    img.putpalette([0, 0, 0, 255, 0, 0] + [0] * 762)
    img.paste(1, (16, 16, 32, 32))
    return img


def _exif6() -> bytes:
    exif = Image.Exif()
    exif[0x0112] = 6
    return exif.tobytes()


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("rgb-key", _png(Image.new("RGB", (48, 48), (0, 0, 0)), transparency=(0, 0, 0))),
        ("l-key", _png(Image.new("L", (48, 48), 0), transparency=0)),
        ("i16-key", _png(Image.new("I;16", (48, 48), 0), transparency=0)),
        ("1bit-key", _png(Image.new("1", (48, 48), 0), transparency=0)),
        ("p-bytes", _png(_palette(), transparency=bytes([0, 255]))),
        ("p-exif", _png(_palette(), transparency=0, exif=_exif6())),
    ],
)
def test_transparent_corner_stays_clear_and_vision_is_white(name: str, data: bytes) -> None:
    out = derive(data)
    assert out is not None, name
    clean = Image.open(io.BytesIO(out.clean)).convert("RGBA")
    assert clean.getpixel((0, 0))[3] == 0, name  # type: ignore[index]
    vision = Image.open(io.BytesIO(out.vision)).convert("RGB")
    assert vision.getpixel((0, 0)) == (255, 255, 255), name
    assert b"eXIf" not in out.clean and b"tEXt" not in out.clean
