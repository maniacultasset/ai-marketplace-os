"""G2: the decoder-free header read (`app.services.image_header`)."""

from __future__ import annotations

import io
import struct
from typing import Any

import pytest
from app.services.image_header import dimensions, sniff
from PIL import Image


def encoded(fmt: str, size: tuple[int, int] = (37, 21), **kw: Any) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, (200, 10, 10)).save(out, format=fmt, **kw)
    return out.getvalue()


@pytest.mark.parametrize(
    ("fmt", "kind", "kw"),
    [
        ("JPEG", "jpeg", {}),
        ("JPEG", "jpeg", {"progressive": True}),
        ("PNG", "png", {}),
    ],
)
def test_real_files_read_their_size(fmt: str, kind: str, kw: dict[str, Any]) -> None:
    data = encoded(fmt, **kw)
    assert sniff(data) == kind
    assert dimensions(data, kind) == (37, 21)


def test_jpeg_with_exif_before_the_frame() -> None:
    exif = Image.Exif()
    exif[0x010F] = "Camera Maker" * 50
    data = encoded("JPEG", exif=exif.tobytes())
    assert dimensions(data, "jpeg") == (37, 21)


@pytest.mark.parametrize(
    ("fmt", "kind"),
    [("GIF", "gif"), ("WEBP", "webp"), ("TIFF", "tiff"), ("BMP", "bmp")],
)
def test_other_formats_are_named(fmt: str, kind: str) -> None:
    assert sniff(encoded(fmt)) == kind


@pytest.mark.parametrize(
    ("data", "kind"),
    [
        (b"<!DOCTYPE html><html></html>", "html"),
        (b"   \n<html>", "html"),
        (b'<svg xmlns="http://www.w3.org/2000/svg"/>', "svg"),
        (b'<?xml version="1.0"?>\n<svg/>', "svg"),
        (b"\xef\xbb\xbf<svg/>", "svg"),
        (b"\x00\x00\x00\x1cftypavif", "heic"),
        (b"plain text", "unknown"),
        (b"", "unknown"),
    ],
)
def test_sniff(data: bytes, kind: str) -> None:
    assert sniff(data) == kind


@pytest.mark.parametrize(
    "data",
    [
        b"\xff\xd8\xff",  # cut after SOI
        b"\xff\xd8\xff\xe0\x00",  # cut inside a length
        b"\xff\xd8\xff\xda\x00\x08",  # scan data before any frame header
        b"\xff\xd8\xff\xd9",  # end of image before any frame header
        b"\xff\xd8\x00\x00",  # not a marker
        b"\xff\xd8\xff\xe0\x00\x01",  # a length under 2
        b"\xff\xd8\xff\xc0\x00\x11\x08\x00",  # a cut frame header
    ],
)
def test_broken_jpeg_headers_have_no_size(data: bytes) -> None:
    assert dimensions(data, "jpeg") is None


def test_broken_png_headers_have_no_size() -> None:
    signature = b"\x89PNG\r\n\x1a\n"
    assert dimensions(signature, "png") is None
    wrong_length = signature + struct.pack(">I", 12) + b"IHDR" + b"\x00" * 13
    assert dimensions(wrong_length, "png") is None
    wrong_type = signature + struct.pack(">I", 13) + b"IDAT" + b"\x00" * 13
    assert dimensions(wrong_type, "png") is None
    assert dimensions(b"GIF89a", "gif") is None
