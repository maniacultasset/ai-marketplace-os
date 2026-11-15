"""What a downloaded file is, and its pixel size, from its bytes alone (G2 sheet photos).

Pure and decoder-free: the worker never decodes an untrusted image in its own process (S17b CP6;
the full decode, with the decompression-bomb guard under RLIMIT, stays in the killable image check
child). This module only reads magic bytes and fixed header fields with `struct`:

- `sniff(data)`: `jpeg`, `png`, `gif`, `webp`, `tiff`, `heic`, `bmp`, `svg`, `html` or `unknown`.
  Text that opens with `<` (after a BOM or whitespace) is `svg` when it names an `<svg` element
  near the start, else `html`.
- `dimensions(data, kind)`: (width, height) from a PNG's IHDR chunk or a JPEG's first SOFn
  segment; None when the header is cut short or malformed.

Only JPEG and PNG are stored (`ACCEPTED`): the upload checks decode JPEG, PNG and TIFF and have
no WebP, and a sheet link is never a reason to widen what the checks read.
"""

from __future__ import annotations

import struct
from typing import Final, Literal

__all__ = ["ACCEPTED", "Kind", "dimensions", "sniff"]

Kind = Literal["jpeg", "png", "gif", "webp", "tiff", "heic", "bmp", "svg", "html", "unknown"]
ACCEPTED: Final = frozenset({"jpeg", "png"})
MIME: Final = {"jpeg": "image/jpeg", "png": "image/png"}

_PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
_HEIF_BRANDS: Final = frozenset(
    {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1", b"avif", b"avis"}
)
# SOF0-SOF15 except DHT (C4), JPG (C8) and DAC (CC)
_SOF: Final = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)
# markers with no length field: TEM, RST0-7, SOI
_STANDALONE: Final = frozenset({0x01, 0xD0, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8})
_TEXT_PEEK: Final = 1024


def sniff(data: bytes) -> Kind:
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(_PNG_SIGNATURE):
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if data[4:8] == b"ftyp" and data[8:12] in _HEIF_BRANDS:
        return "heic"
    if data[:2] == b"BM":
        return "bmp"
    head = data[:_TEXT_PEEK]
    if head.startswith(b"\xef\xbb\xbf"):
        head = head[3:]
    text = head.lstrip().lower()
    if text.startswith(b"<"):
        return "svg" if b"<svg" in text else "html"
    return "unknown"


def dimensions(data: bytes, kind: str) -> tuple[int, int] | None:
    if kind == "png":
        return _png(data)
    if kind == "jpeg":
        return _jpeg(data)
    return None


def _png(data: bytes) -> tuple[int, int] | None:
    # signature (8), then the first chunk: length (4) = 13, type (4) = IHDR, width (4), height (4)
    if len(data) < 24 or data[12:16] != b"IHDR":
        return None
    (length,) = struct.unpack(">I", data[8:12])
    if length != 13:
        return None
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def _jpeg(data: bytes) -> tuple[int, int] | None:
    i, n = 2, len(data)
    while i < n:
        if data[i] != 0xFF:
            return None  # a segment must start on a marker
        while i < n and data[i] == 0xFF:  # fill bytes
            i += 1
        if i >= n:
            return None
        marker = data[i]
        i += 1
        if marker in _STANDALONE:
            continue
        if marker in (0xD9, 0xDA):  # EOI or SOS before any frame header
            return None
        if i + 2 > n:
            return None
        (length,) = struct.unpack(">H", data[i : i + 2])
        if length < 2:
            return None
        if marker in _SOF:
            if length < 7 or i + 7 > n:
                return None
            height, width = struct.unpack(">HH", data[i + 3 : i + 7])
            return width, height
        i += length
    return None
