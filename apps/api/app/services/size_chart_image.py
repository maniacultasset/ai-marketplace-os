"""The size-chart image (SI7, AC8): a deterministic Pillow render of the seller's size chart and
fabric, zero tokens.

A white 1500 px square with a title ("Garment measurements, cm"), the table of the columns that
hold at least one value, and a fabric line ("Fabric: 95% cotton, 5% elastane") when the fabric is
known. No logo and no brand name. The bundled font has Latin-1 glyphs only, so a label outside
printable Latin-1 is refused (`ChartRefused`, CP-D11); seller text is drawn as given, never used
as a format string.

Pure library: no database, settings, logger or I/O.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Final

from mkos_schemas.size_chart_read import (
    MAX_SIZE_CHART_READ_ROWS,
    MAX_SIZE_CHART_SIZE_CHARS,
    SIZE_CHART_MEASURE_KEYS,
)
from PIL import Image, ImageDraw, ImageFont

__all__ = [
    "COLUMNS",
    "ChartRefused",
    "Layout",
    "layout_size_chart",
    "render_size_chart",
]

COLUMNS: Final = SIZE_CHART_MEASURE_KEYS
SIDE: Final = 1500
MARGIN: Final = 90
MAX_ROWS: Final = MAX_SIZE_CHART_READ_ROWS
MAX_SIZE_LABEL: Final = MAX_SIZE_CHART_SIZE_CHARS
MAX_FIBRE_NAME: Final = 64
MAX_FIBRES: Final = 10
FONT_MAX: Final = 44
FONT_MIN: Final = 12
CELL_PAD: Final = 0.9  # of the font size, each side of a cell
ROW_HEIGHT: Final = 1.7  # of the font size
JPEG_QUALITY: Final = 92
INK: Final = (20, 20, 20)
RULE: Final = (200, 200, 200)
WHITE: Final = (255, 255, 255)
_SPACES: Final = str.maketrans({"\n": " ", "\r": " ", "\t": " "})


class ChartRefused(Exception):  # noqa: N818 - CODE-PLAN name; a verdict, not a failure
    """The chart cannot be drawn (a label the font cannot show, or a table past the bounds)."""


@dataclass(frozen=True, slots=True)
class Layout:
    title: str
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    fabric: str | None
    font_size: int
    col_widths: tuple[int, ...]
    row_height: int


def _text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        raise ChartRefused("a label is not text")
    text = " ".join(value.translate(_SPACES).split())
    if not text or len(text) > limit:
        raise ChartRefused("a label is empty or too long")
    if any(not (0x20 <= ord(c) <= 0x7E or 0xA0 <= ord(c) <= 0xFF) for c in text):
        raise ChartRefused("a label is outside printable Latin-1")
    return text


def _number(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ChartRefused("a measurement is not a number")
    return "%g" % value  # noqa: UP031 - a number, never seller text


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    return ImageFont.load_default(size=size)


def _width(font: ImageFont.FreeTypeFont | ImageFont.ImageFont, text: str) -> int:
    left, _, right, _ = font.getbbox(text)
    return math.ceil(right - left)


def layout_size_chart(
    chart: Mapping[str, Any], fibres: Sequence[Mapping[str, Any]] | None
) -> Layout:
    unit, basis, rows = chart.get("unit"), chart.get("basis"), chart.get("rows")
    if unit not in ("cm", "in") or basis not in ("garment", "body"):
        raise ChartRefused("unknown unit or basis")
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ROWS:
        raise ChartRefused("the table has no rows or too many")
    for row in rows:
        if not isinstance(row, Mapping) or set(row) - {"size", *COLUMNS}:
            raise ChartRefused("a row holds an unknown column")
    present = tuple(c for c in COLUMNS if any(row.get(c) is not None for row in rows))
    if not present:
        raise ChartRefused("the table has no measurement")
    headers = ("Size", *(c.capitalize() for c in present))
    cells = tuple(
        (
            _text(row.get("size"), MAX_SIZE_LABEL),
            *(("-" if row.get(c) is None else _number(row.get(c))) for c in present),
        )
        for row in rows
    )
    fabric = None
    if fibres:
        if len(fibres) > MAX_FIBRES:
            raise ChartRefused("too many fibres")
        parts = [
            f"{_number(f.get('percent'))}% {_text(f.get('fibre'), MAX_FIBRE_NAME)}" for f in fibres
        ]
        fabric = "Fabric: " + ", ".join(parts)
    title = f"{'Garment' if basis == 'garment' else 'Body'} measurements, {unit}"
    inner = SIDE - 2 * MARGIN
    for size in range(FONT_MAX, FONT_MIN - 1, -1):
        font = _font(size)
        pad = round(size * CELL_PAD)
        widths = tuple(
            max(_width(font, line[i]) for line in (headers, *cells)) + 2 * pad
            for i in range(len(headers))
        )
        row_h = round(size * ROW_HEIGHT)
        lines = 2 + len(cells) + (1 if fabric else 0) + 1  # title, header, rows, fabric, gaps
        fits_text = _width(_font(round(size * 1.15)), title) <= inner and (
            fabric is None or _width(font, fabric) <= inner
        )
        if sum(widths) <= inner and lines * row_h <= inner and fits_text:
            return Layout(title, headers, cells, fabric, size, widths, row_h)
    raise ChartRefused("the table does not fit")


def render_size_chart(
    chart: Mapping[str, Any], fibres: Sequence[Mapping[str, Any]] | None
) -> bytes:
    lay = layout_size_chart(chart, fibres)
    img = Image.new("RGB", (SIDE, SIDE), WHITE)
    draw = ImageDraw.Draw(img)
    font = _font(lay.font_size)
    title_font = _font(round(lay.font_size * 1.15))
    table_w = sum(lay.col_widths)
    lines = 2 + len(lay.rows) + (1 if lay.fabric else 0) + 1
    top = MARGIN + (SIDE - 2 * MARGIN - lines * lay.row_height) // 2
    left = (SIDE - table_w) // 2
    draw.text((left, top), lay.title, fill=INK, font=title_font)
    y = top + lay.row_height * 2
    for index, line in enumerate((lay.headers, *lay.rows)):
        x = left
        for width, text in zip(lay.col_widths, line, strict=True):
            draw.text(
                (x + width // 2, y + lay.row_height // 2), text, fill=INK, font=font, anchor="mm"
            )
            x += width
        if index == 0:
            draw.line(
                (left, y + lay.row_height, left + table_w, y + lay.row_height), fill=INK, width=2
            )
        else:
            draw.line(
                (left, y + lay.row_height, left + table_w, y + lay.row_height), fill=RULE, width=1
            )
        y += lay.row_height
    if lay.fabric:
        draw.text((left, y + lay.row_height // 2), lay.fabric, fill=INK, font=font, anchor="lm")
    buf = BytesIO()
    img.save(buf, "JPEG", quality=JPEG_QUALITY, optimize=False)
    return buf.getvalue()
