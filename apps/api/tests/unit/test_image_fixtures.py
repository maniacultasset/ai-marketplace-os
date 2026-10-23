"""The synthetic image fixtures are what they claim to be, and deterministic."""

from __future__ import annotations

import struct
from collections.abc import Callable
from io import BytesIO

import pytest
from PIL import Image
from tests import image_fixtures as fx


def _open(data: bytes) -> Image.Image:
    img = Image.open(BytesIO(data))
    img.load()
    return img


@pytest.mark.parametrize(
    ("build", "fmt", "mode"),
    [
        (lambda: fx.jpeg(40, 30), "JPEG", "RGB"),
        (lambda: fx.jpeg(40, 30, progressive=True), "JPEG", "RGB"),
        (lambda: fx.png(40, 30), "PNG", "RGB"),
        (lambda: fx.png(40, 30, mode="RGBA", alpha=0), "PNG", "RGBA"),
        (lambda: fx.png(40, 30, mode="L"), "PNG", "L"),
        (lambda: fx.png(40, 30, mode="LA"), "PNG", "LA"),
        (lambda: fx.png(40, 30, mode="P"), "PNG", "P"),
        (lambda: fx.png(40, 30, mode="1"), "PNG", "1"),
        (lambda: fx.png(40, 30, mode="I;16"), "PNG", "I;16"),
        (lambda: fx.tiff(40, 30), "TIFF", "RGB"),
        (lambda: fx.tiff(40, 30, mode="CMYK"), "TIFF", "CMYK"),
    ],
)
def test_builders_reopen_as_claimed(build: Callable[[], bytes], fmt: str, mode: str) -> None:
    img = _open(build())
    assert (img.format, img.mode, img.size) == (fmt, mode, (40, 30))


def test_canvas_layout() -> None:
    img = fx.canvas(100, 50, fill=0.6)
    assert img.getpixel((0, 0)) == fx.WHITE
    assert img.getpixel((50, 25)) == fx.DARK
    assert fx.canvas(10, 10, fill=0).getcolors() == [(100, fx.WHITE)]


def test_png_transparency_and_text() -> None:
    assert _open(fx.png(10, 10, mode="RGBA", alpha=0)).getpixel((0, 0)) == (255, 255, 255, 0)
    p = _open(fx.png(10, 10, mode="P", transparent_product=True))
    assert "transparency" in p.info
    assert len(fx.png(10, 10, text_chunk_bytes=2_000_000)) < 50_000
    i16 = _open(fx.png(10, 10, mode="I;16"))
    assert i16.getpixel((0, 0)) == 65535


def test_tiff_frames() -> None:
    img = Image.open(BytesIO(fx.tiff(20, 20, frames=3)))
    assert getattr(img, "n_frames", 1) == 3


def test_png_header_only_claims_size() -> None:
    data = fx.png_header_only(30000, 30000)
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    assert data[12:16] == b"IHDR"
    assert struct.unpack(">II", data[16:24]) == (30000, 30000)
    assert fx.png_header_only(8, 8, mode="L")[25] == 0


def test_exif_orientation_reads_back() -> None:
    assert _open(fx.jpeg(20, 10, exif_orientation=6)).getexif().get(0x0112) == 6


def test_exif_orientation_duplicate_tags_written() -> None:
    exif = _open(fx.jpeg(20, 10, exif_orientation=(6, 1))).info["exif"]
    assert exif.count(b"\x01\x12\x00\x03") == 2


def test_tiff_orientation_tags_decodes() -> None:
    img = _open(fx.tiff_orientation_tags(20, 10, (1,)))
    assert (img.format, img.mode, img.size) == ("TIFF", "RGB", (20, 10))
    assert img.getpixel((0, 0)) == fx.WHITE
    assert img.getpixel((10, 5)) == fx.DARK


def test_tiff_orientation_tags_written_in_order() -> None:
    raw = fx.tiff_orientation_tags(20, 10, (6, 1))
    assert raw.count(struct.pack("<HHIH", 0x0112, 3, 1, 6)) == 1
    assert raw.index(struct.pack("<HHIH", 0x0112, 3, 1, 6)) < raw.index(
        struct.pack("<HHIH", 0x0112, 3, 1, 1)
    )


def test_truncated() -> None:
    assert fx.truncated(b"abcdef", 3) == b"abc"


@pytest.mark.parametrize(
    ("name", "prefix"),
    [
        ("gif", b"GIF8"),
        ("bmp", b"BM"),
        ("webp", b"RIFF"),
        ("svg", b"<svg"),
        ("bigtiff", b"II+\x00"),
    ],
)
def test_magic(name: str, prefix: bytes) -> None:
    assert fx.magic(name).startswith(prefix)


def test_magic_real_images_decode() -> None:
    assert {_open(fx.magic(n)).format for n in ("gif", "bmp", "webp")} == {"GIF", "BMP", "WEBP"}


@pytest.mark.parametrize("n", [1, 3, 10])
def test_jpeg_scans_count(n: int) -> None:
    data = fx.jpeg_scans(n, stuffing=True, restart=True)
    assert data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9")
    assert data.count(b"\xff\xda") == n


def test_jpeg_with_exif_thumbnail_holds_sos_in_app1() -> None:
    data = fx.jpeg_with_exif_thumbnail()
    app1 = data.index(b"\xff\xe1")
    (length,) = struct.unpack(">H", data[app1 + 2 : app1 + 4])
    assert b"\xff\xda" in data[app1 + 4 : app1 + 2 + length]
    assert _open(data).size == (64, 64)


@pytest.mark.parametrize(
    "build",
    [
        lambda: fx.jpeg(50, 40, exif_orientation=8),
        lambda: fx.png(50, 40, mode="P"),
        lambda: fx.tiff(50, 40, frames=2),
        lambda: fx.jpeg_with_exif_thumbnail(),
        lambda: fx.magic("webp"),
    ],
)
def test_builders_deterministic(build: Callable[[], bytes]) -> None:
    assert build() == build()
