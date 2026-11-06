"""SI1 tester round 1: property and fuzz tests on the pure seller-intake seams (AC2, AC3, AC9,
AC11, AC19). They hold on the current tree; the failures found are under
`docs/plans/si1-seller-intake/repro/`."""

from __future__ import annotations

import io
import itertools
import random
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from app.core.config import _image_public_errors
from app.repositories.images import ListingImageRow
from app.services.composition import CompositionError, normalized, parse_composition
from app.services.generation_images import MAX_LISTING_IMAGES, order_for_listing, pick_context
from app.services.image_derive import derive
from app.services.intake_values import (
    MoneyError,
    SizeChartError,
    normalize_size_chart,
    parse_money,
)
from hypothesis import given, settings
from hypothesis import strategies as st
from mkos_schemas.enums import ImageSlot
from PIL import Image

# ---- composition (AC2) ----

_FIBRES = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Lu"), max_codepoint=0x24F),
    min_size=1,
    max_size=20,
)


@st.composite
def _composition(draw: Any) -> tuple[str, list[tuple[str, int]]]:
    names = draw(st.lists(_FIBRES, min_size=1, max_size=10, unique_by=str.casefold))
    if len(names) == 1:
        cuts: list[int] = []
    else:
        size = len(names) - 1
        cuts = sorted(draw(st.lists(st.integers(1, 99), min_size=size, max_size=size, unique=True)))
    bounds = [0, *cuts, 100]
    percents = [b - a for a, b in itertools.pairwise(bounds)]
    pairs = list(zip(names, percents, strict=True))
    front = draw(st.booleans())
    sep = draw(st.sampled_from([", ", "/", "; ", " + ", " and ", ","]))
    parts = [f"{p}% {n}" if front else f"{n} {p}%" for n, p in pairs]
    return sep.join(parts), pairs


@settings(max_examples=200, deadline=None)
@given(_composition())
def test_composition_round_trip(case: tuple[str, list[tuple[str, int]]]) -> None:
    text, pairs = case
    out = normalized(parse_composition(text))
    assert [(o["fibre"], o["percent"]) for o in out] == [(n.casefold(), p) for n, p in pairs]
    assert sum(Decimal(str(o["percent"])) for o in out) == 100


@settings(max_examples=300, deadline=None)
@given(st.text(max_size=200))
def test_composition_only_refuses_with_composition_error(text: str) -> None:
    try:
        fibres = parse_composition(text)
    except CompositionError:
        return
    assert 1 <= len(fibres) <= 10
    assert sum(f.percent for f in fibres) == 100
    assert all(0 < f.percent <= 100 for f in fibres)


@pytest.mark.parametrize("text", ["0% Cotton, 100% Polyester", "101% Cotton", "59% A, 40% B"])
def test_composition_edges_refused(text: str) -> None:
    with pytest.raises(CompositionError):
        parse_composition(text)


# ---- money and size chart (AC1, AC3) ----


@settings(max_examples=300, deadline=None)
@given(st.one_of(st.text(max_size=20), st.integers(), st.floats(), st.none()))
def test_money_only_refuses_with_money_error(value: object) -> None:
    try:
        money = parse_money(value)
    except MoneyError:
        return
    assert isinstance(value, str)
    assert money > 0 and money == money.quantize(Decimal("0.01"))
    assert money < Decimal("10000000000")  # Numeric(12,2)


@pytest.mark.parametrize("bad", ["0", "0.00", "-1", "1e3", "1.234", " 1", "1 ", "01", "\uff11"])
def test_money_bad_strings(bad: str) -> None:
    with pytest.raises(MoneyError):
        parse_money(bad)


_JSONISH = st.recursive(
    st.one_of(st.none(), st.booleans(), st.integers(), st.floats(), st.text(max_size=8)),
    lambda inner: st.one_of(
        st.lists(inner, max_size=4), st.dictionaries(st.text(max_size=8), inner, max_size=4)
    ),
    max_leaves=12,
)


@settings(max_examples=300, deadline=None)
@given(
    st.fixed_dictionaries(
        {
            "unit": st.sampled_from(["cm", "in", "mm", None]),
            "basis": st.sampled_from(["garment", "body", "x"]),
            "rows": _JSONISH,
        }
    )
)
def test_size_chart_only_refuses_with_size_chart_error(raw: dict[str, Any]) -> None:
    try:
        out = normalize_size_chart(raw)
    except SizeChartError:
        return
    assert 1 <= len(out["rows"]) <= 40
    assert all(len(r) >= 2 for r in out["rows"])


@pytest.mark.parametrize("value", [float("nan"), float("inf"), Decimal("NaN"), True, 0, 500.05])
def test_size_chart_bad_measure(value: object) -> None:
    raw = {"unit": "cm", "basis": "garment", "rows": [{"size": "M", "chest": value}]}
    with pytest.raises(SizeChartError):
        normalize_size_chart(raw)


# ---- derivatives (AC9) ----


@settings(max_examples=150, deadline=None)
@given(st.binary(max_size=4096))
def test_derive_never_raises_on_random_bytes(data: bytes) -> None:
    out = derive(data)
    assert out is None or (out.clean and out.vision and out.vision_w <= 1024)


def test_derive_png_text_chunk_and_exif_dropped() -> None:
    from PIL import PngImagePlugin

    meta = PngImagePlugin.PngInfo()
    meta.add_text("Comment", "GPS 12.97,77.59 owner@example.test")
    exif = Image.Exif()
    exif[0x0112] = 6
    img = Image.new("RGB", (40, 20), (200, 10, 10))
    buf = io.BytesIO()
    img.save(buf, format="PNG", pnginfo=meta, exif=exif)
    out = derive(buf.getvalue())
    assert out is not None and out.clean_mime == "image/png"
    assert b"owner@example.test" not in out.clean and b"eXIf" not in out.clean
    clean = Image.open(io.BytesIO(out.clean))
    assert clean.size == (20, 40)  # orientation 6 applied


# ---- listing order (AC11) ----

T0 = datetime(2026, 11, 7, tzinfo=UTC)
_CW = [uuid.UUID(int=10_000 + i) for i in range(4)]


@st.composite
def _rows(draw: Any) -> list[ListingImageRow]:
    count = draw(st.integers(0, 40))
    rows = []
    for i in range(count):
        cw = draw(st.sampled_from([None, *_CW]))
        rows.append(
            ListingImageRow(
                image_id=uuid.UUID(int=i + 1),
                product_id=uuid.UUID(int=999),
                slot=draw(st.sampled_from([s.value for s in ImageSlot])),
                colourway_id=cw,
                position=draw(st.integers(0, 30)),
                clean_content_hash=draw(st.sampled_from([None, "a" * 64])),
                vision={"w": 10, "h": 10},
                colourway_position=None if cw is None else _CW.index(cw),
                created_at=T0 + timedelta(seconds=draw(st.integers(0, 50))),
            )
        )
    return rows


@settings(max_examples=200, deadline=None)
@given(_rows(), st.randoms())
def test_listing_order_is_input_order_independent(rows: list[ListingImageRow], rnd: random.Random) -> None:  # fmt: skip
    refs = order_for_listing(rows)
    shuffled = list(rows)
    rnd.shuffle(shuffled)
    assert order_for_listing(shuffled) == refs
    assert len(refs) <= MAX_LISTING_IMAGES
    assert ImageSlot.PACKAGING_LABEL not in {r.slot for r in refs}
    assert [r.position for r in refs] == list(range(len(refs)))
    assert len({r.image_id for r in refs}) == len(refs)
    picked = pick_context(rows)
    assert len(picked) <= 7
    assert all(p.clean_content_hash is not None for p in picked)
    assert sum(1 for p in picked if p.slot == ImageSlot.PACKAGING_LABEL.value) <= 1


# ---- public base URL (AC19) ----


def _errors(base: str, app_env: str = "production") -> list[str]:
    return _image_public_errors(
        SimpleNamespace(  # type: ignore[arg-type]
            gcs_bucket_images_public="b",
            image_public_base_url=base,
            app_env=app_env,
            s3_endpoint_url="https://storage.googleapis.com",
        )
    )


@pytest.mark.parametrize(
    "base",
    [
        "http://images.kuinn.in",
        "https:///x",
        "https://images.kuinn.in?x=1",
        "https://images.kuinn.in#f",
        "https://images.kuinn.in/",
        "https://u:p@images.kuinn.in",
        "https://@images.kuinn.in",
        "https://STORAGE.googleapis.com",
        "ftp://images.kuinn.in",
    ],
)
@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_public_base_url_refused_off_local(base: str, app_env: str) -> None:
    assert _errors(base, app_env)


def test_public_base_url_good() -> None:
    assert _errors("https://images.kuinn.in") == []
    assert _errors("https://cdn.kuinn.in/img") == []
