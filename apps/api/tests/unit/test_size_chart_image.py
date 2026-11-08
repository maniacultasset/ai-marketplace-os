"""SI7 T7: the size-chart image (AC8; CP-D8 structural asserts, not a golden hash)."""

from __future__ import annotations

from io import BytesIO
from typing import Any

import pytest
from app.services.size_chart_image import (
    COLUMNS,
    MARGIN,
    SIDE,
    ChartRefused,
    layout_size_chart,
    render_size_chart,
)
from PIL import Image

CHART: dict[str, Any] = {
    "unit": "cm",
    "basis": "garment",
    "rows": [
        {"size": "S", "chest": 96, "length": 70.5},
        {"size": "M", "chest": 101, "length": 72},
        {"size": "L", "chest": 106, "length": 74},
    ],
}
FIBRES = [{"fibre": "cotton", "percent": 95}, {"fibre": "elastane", "percent": 5}]


def _decode(data: bytes) -> Image.Image:
    img = Image.open(BytesIO(data))
    assert img.format == "JPEG"
    return img.convert("RGB")


def test_a_white_square_with_a_table() -> None:
    img = _decode(render_size_chart(CHART, FIBRES))
    assert img.size == (SIDE, SIDE)
    band = 30
    for box in ((0, 0, SIDE, band), (0, SIDE - band, SIDE, SIDE), (0, 0, band, SIDE)):
        assert img.crop(box).getextrema() == ((255, 255),) * 3
    inner = img.crop((MARGIN, MARGIN, SIDE - MARGIN, SIDE - MARGIN))
    low, _ = inner.convert("L").getextrema()
    assert isinstance(low, int) and low < 100  # ink is present


def test_same_input_same_bytes() -> None:
    assert render_size_chart(CHART, FIBRES) == render_size_chart(CHART, FIBRES)


def test_only_present_columns_are_drawn() -> None:
    lay = layout_size_chart(CHART, None)
    assert lay.headers == ("Size", "Chest", "Length")
    assert lay.rows[0] == ("S", "96", "70.5")
    assert lay.title == "Garment measurements, cm"
    assert lay.fabric is None


def test_fabric_line() -> None:
    assert layout_size_chart(CHART, FIBRES).fabric == "Fabric: 95% cotton, 5% elastane"
    body = {**CHART, "basis": "body", "unit": "in"}
    assert layout_size_chart(body, None).title == "Body measurements, in"


def test_forty_rows_and_every_column_fit() -> None:
    rows = [
        {"size": f"XXL-{i:02d}-EXTRA-LONG-TT", **dict.fromkeys(COLUMNS, 123.5)} for i in range(40)
    ]
    chart = {"unit": "cm", "basis": "garment", "rows": rows}
    lay = layout_size_chart(chart, FIBRES)
    assert len(lay.headers) == 8 and sum(lay.col_widths) <= SIDE - 2 * MARGIN
    assert len(lay.rows[0][0]) == 20
    img = _decode(render_size_chart(chart, FIBRES))
    assert img.crop((0, 0, SIDE, 20)).getextrema() == ((255, 255),) * 3


@pytest.mark.parametrize(
    "chart",
    [
        {**CHART, "rows": [{"size": "चिकनकारी", "chest": 90}]},
        {**CHART, "rows": [{"size": "S" * 21, "chest": 90}]},
        {**CHART, "rows": [{"size": "S", "neck": 40}]},
        {**CHART, "rows": [{"size": "S"}]},
        {**CHART, "rows": [{"size": "S", "chest": float("nan")}]},
        {**CHART, "rows": [{"size": "S", "chest": 90}] * 41},
        {**CHART, "rows": []},
        {**CHART, "unit": "mm"},
    ],
    ids=[
        "devanagari",
        "long-label",
        "unknown-column",
        "no-value",
        "nan",
        "41-rows",
        "empty",
        "unit",
    ],
)
def test_refused(chart: dict[str, Any]) -> None:
    with pytest.raises(ChartRefused):
        render_size_chart(chart, None)


def test_non_latin_fibre_refused() -> None:
    with pytest.raises(ChartRefused):
        render_size_chart(CHART, [{"fibre": "कपास", "percent": 100}])


def test_seller_text_is_literal() -> None:
    chart = {**CHART, "rows": [{"size": "{0}%s\n", "chest": 90}]}
    lay = layout_size_chart(chart, None)
    assert lay.rows[0][0] == "{0}%s"
    assert render_size_chart(chart, None)
