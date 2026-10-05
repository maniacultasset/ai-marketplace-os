"""Tester attacks on the image checks: JPEG marker floods, exotic pixel modes, trailers after
EOI, JPEG-in-TIFF and transparent white."""

from __future__ import annotations

import struct

import pytest
from PIL import Image
from tests import image_fixtures as fx
from tests.image_fixtures import codes, encode, image_rule, names, run

WHITE_BG = [image_rule("background", rgb=[255, 255, 255])]
FLOOD = 2_000_000


def _seg(marker: int, payload: bytes) -> bytes:
    return bytes((0xFF, marker)) + struct.pack(">H", len(payload) + 2) + payload


_HEAD = (
    b"\xff\xd8"
    + _seg(0xDB, b"\x00" + b"\x01" * 64)
    + _seg(0xC2, struct.pack(">BHHB", 8, 8, 8, 1) + b"\x01\x11\x00")
    + _seg(0xC4, b"\x00\x01" + b"\x00" * 16)
    + _seg(0xDA, b"\x01\x01\x00\x00\x3f\x00")
)


@pytest.mark.parametrize(
    "data",
    [
        _HEAD + b"\xff\xd0" * (FLOOD // 2) + b"\xff\xd9",  # RSTn flood in entropy data
        _HEAD + b"\xff\x00" * (FLOOD // 2) + b"\xff\xd9",  # stuffing flood
        b"\xff\xd8" + b"\xff" * FLOOD + b"\xd9",  # fill-byte flood
        b"\xff\xd8" + b"\xff\xfe\x00\x02" * (FLOOD // 4) + b"\xff\xd9",  # empty COM flood
        b"\xff\xd8" + b"\xff\x01" * (FLOOD // 2) + b"\xff\xd9",  # TEM flood
    ],
    ids=["rst", "stuffing", "fill", "com", "tem"],
)
def test_image_checks_hostile_marker_flood(data: bytes) -> None:
    report = run(data, WHITE_BG)
    assert names(report) == ["format", "corruption"]
    assert codes(report) == ["CORRUPT"]
    assert report.pass_ is False


def _white(mode: str) -> Image.Image:
    if mode in ("I", "I;16B"):
        return Image.new(mode, (120, 120), 65535)
    if mode == "F":
        return Image.new("F", (120, 120), 65535.0)
    if mode == "LA":
        return Image.new("LA", (120, 120), (255, 255))
    if mode == "PA":
        return Image.new("RGBA", (120, 120), (255, 255, 255, 255)).convert("PA")
    return Image.new("RGB", (120, 120), (255, 255, 255)).convert(mode)


@pytest.mark.parametrize("mode", ["LAB", "CMYK", "I", "F", "I;16B", "LA", "PA", "RGBX"])
def test_background_exotic_modes_white_tiff_passes(mode: str) -> None:
    report = run(encode(_white(mode), "TIFF"), WHITE_BG)
    assert names(report)[-1] == "background"
    assert report.pass_ is True, codes(report)


def test_background_cmyk_jpeg_white_passes() -> None:
    data = encode(Image.new("CMYK", (120, 120), (0, 0, 0, 0)), "JPEG")
    assert run(data, WHITE_BG).pass_ is True


def test_background_jpeg_compressed_tiff_passes() -> None:
    data = encode(fx.canvas(300, 300), "TIFF", compression="jpeg")
    report = run(data, WHITE_BG)
    assert report.pass_ is True
    assert names(report)[-1] == "background"


def test_background_transparent_white_png_fails() -> None:
    img = Image.new("RGB", (200, 200), (255, 255, 255))
    report = run(encode(img, "PNG", transparency=(255, 255, 255)), WHITE_BG)
    assert codes(report) == ["BACKGROUND_OFF"]


def test_jpeg_scan_trailer_after_eoi_ignored() -> None:
    """SOS segments after EOI are never decoded, so they do not count towards the scan cap."""
    report = run(fx.jpeg(300, 300) + fx.jpeg_scans(500))
    assert report.pass_ is True
    assert "corruption" in names(report)


def test_format_rule_aliases_case_insensitive() -> None:
    rules = [image_rule("format", allowed_formats=["JPG", "Tif"])]
    assert run(fx.jpeg(200, 200), rules).pass_ is True
    assert codes(run(fx.png(200, 200), rules)) == ["FORMAT_UNSUPPORTED"]
