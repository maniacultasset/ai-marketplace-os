"""Synthetic image fixtures for the image checks tests. Every builder is deterministic (same
arguments give the same bytes) and generates its image in memory; no binary file is committed."""

from __future__ import annotations

import itertools
import struct
import zlib
from collections.abc import Sequence
from datetime import UTC, datetime
from io import BytesIO
from typing import Any
from uuid import UUID

from mkos_schemas.enums import ImageSlot, Marketplace
from mkos_schemas.images import ImageReport
from mkos_schemas.rules import Rule
from PIL import Image, PngImagePlugin

RGB = tuple[int, int, int]
WHITE: RGB = (255, 255, 255)
DARK: RGB = (30, 30, 30)

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_COLOUR_TYPES = {"L": 0, "RGB": 2, "RGBA": 6}


def canvas(
    w: int, h: int, *, border: RGB = WHITE, product: RGB = DARK, fill: float = 0.6
) -> Image.Image:
    """An RGB image of colour `border` with a centred `product` rectangle covering `fill` of each
    side (0 = no product)."""
    img = Image.new("RGB", (w, h), border)
    pw, ph = int(w * fill), int(h * fill)
    if pw > 0 and ph > 0:
        left, top = (w - pw) // 2, (h - ph) // 2
        img.paste(product, (left, top, left + pw, top + ph))
    return img


def encode(img: Image.Image, fmt: str, **save: Any) -> bytes:
    buf = BytesIO()
    img.save(buf, fmt, **save)
    return buf.getvalue()


def jpeg(
    w: int,
    h: int,
    *,
    border: RGB = WHITE,
    product: RGB = DARK,
    fill: float = 0.6,
    quality: int = 90,
    exif_orientation: object = None,
    progressive: bool = False,
) -> bytes:
    img = canvas(w, h, border=border, product=product, fill=fill)
    save: dict[str, Any] = {"quality": quality, "progressive": progressive}
    if isinstance(exif_orientation, str):  # Pillow writes the tag only as SHORT; hand-build ASCII
        value = exif_orientation.encode("ascii")[:3].ljust(4, b"\x00")
        entry = struct.pack(">HHI", 0x0112, 2, 2) + value
        save["exif"] = b"Exif\x00\x00MM\x00*" + struct.pack(">IH", 8, 1) + entry + b"\x00" * 4
    elif isinstance(exif_orientation, tuple):  # one SHORT entry per value: a duplicated tag
        entries = b"".join(struct.pack(">HHIH2x", 0x0112, 3, 1, v) for v in exif_orientation)
        head = struct.pack(">IH", 8, len(exif_orientation))
        save["exif"] = b"Exif\x00\x00MM\x00*" + head + entries + b"\x00" * 4
    elif exif_orientation is not None:
        exif = Image.Exif()
        exif[0x0112] = exif_orientation
        save["exif"] = exif.tobytes()
    return encode(img, "JPEG", **save)


def _to_mode(img: Image.Image, mode: str) -> Image.Image:
    if mode == "RGB":
        return img
    if mode == "1":
        return img.convert("1", dither=Image.Dither.NONE)
    if mode == "I;16":
        wide = img.convert("L").convert("I").point(lambda v: v * 257)
        return wide.convert("I;16")
    if mode == "P":
        return img.convert("P", palette=Image.Palette.ADAPTIVE, colors=16)
    return img.convert(mode)


def png(
    w: int,
    h: int,
    *,
    mode: str = "RGB",
    border: RGB = WHITE,
    product: RGB = DARK,
    fill: float = 0.6,
    alpha: int = 255,
    text_chunk_bytes: int = 0,
    transparent_product: bool = False,
) -> bytes:
    """`alpha` applies to the whole image in RGBA/LA; `transparent_product` adds a tRNS entry for
    the product colour of a P image; `text_chunk_bytes` adds a zTXt chunk of that many characters.
    """
    img = _to_mode(canvas(w, h, border=border, product=product, fill=fill), mode)
    save: dict[str, Any] = {}
    if mode in ("RGBA", "LA"):
        img.putalpha(alpha)
    if mode == "P" and transparent_product:
        save["transparency"] = img.getpixel((w // 2, h // 2))
    if text_chunk_bytes:
        info = PngImagePlugin.PngInfo()
        info.add_text("comment", "a" * text_chunk_bytes, zip=True)
        save["pnginfo"] = info
    return encode(img, "PNG", **save)


def tiff(
    w: int,
    h: int,
    *,
    frames: int = 1,
    mode: str = "RGB",
    border: RGB = WHITE,
    product: RGB = DARK,
    fill: float = 0.6,
) -> bytes:
    """Frame 0 is the canvas; later frames are solid black (judging them would fail)."""
    img = _to_mode(canvas(w, h, border=border, product=product, fill=fill), mode)
    if frames == 1:
        return encode(img, "TIFF")
    rest = [Image.new(img.mode, img.size) for _ in range(frames - 1)]
    return encode(img, "TIFF", save_all=True, append_images=rest)


def tiff_orientation_tags(w: int, h: int, tags: Sequence[int]) -> bytes:
    """An uncompressed RGB TIFF of the canvas whose IFD0 carries one Orientation (0x0112) SHORT
    entry per value of `tags`, in order (Pillow's writer keeps one entry per tag, so hand-built)."""
    pixels = canvas(w, h).tobytes()
    entries: list[tuple[int, int, int]] = [  # (tag, type 3=SHORT 4=LONG, value or offset)
        (0x0100, 3, w),
        (0x0101, 3, h),
        (0x0102, 3, 0),  # BitsPerSample 8,8,8: offset patched below
        (0x0103, 3, 1),  # no compression
        (0x0106, 3, 2),  # RGB
        (0x0111, 4, 0),  # StripOffsets: patched below
        *((0x0112, 3, v) for v in tags),
        (0x0115, 3, 3),
        (0x0116, 4, h),
        (0x0117, 4, len(pixels)),
    ]
    bps_at = 8 + 2 + 12 * len(entries) + 4
    data_at = bps_at + 8
    ifd = struct.pack("<H", len(entries))
    for tag, kind, value in entries:
        count = 3 if tag == 0x0102 else 1
        value = {0x0102: bps_at, 0x0111: data_at}.get(tag, value)
        ifd += struct.pack(
            "<HHIH2x" if kind == 3 and count == 1 else "<HHII", tag, kind, count, value
        )
    bps = struct.pack("<HHH2x", 8, 8, 8)
    return b"II*\x00" + struct.pack("<I", 8) + ifd + b"\x00" * 4 + bps + pixels


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)


def png_header_only(w: int, h: int, *, mode: str = "RGB") -> bytes:
    """A PNG claiming `w` x `h` with no pixel data (one empty IDAT); never decodes."""
    ihdr = struct.pack(">IIBBBBB", w, h, 8, _PNG_COLOUR_TYPES[mode], 0, 0, 0)
    return (
        _PNG_SIGNATURE
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", b"")
        + _png_chunk(b"IEND", b"")
    )


def truncated(data: bytes, keep: int) -> bytes:
    return data[:keep]


def magic(name: str) -> bytes:
    """Bytes of a format the library does not decode."""
    small = canvas(16, 16)
    if name == "gif":
        return encode(small, "GIF")
    if name == "bmp":
        return encode(small, "BMP")
    if name == "webp":
        return encode(small, "WEBP", lossless=True)
    if name == "svg":
        return b'<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16"></svg>'
    if name == "bigtiff":
        return b"II+\x00\x08\x00\x00\x00" + b"\x10\x00\x00\x00\x00\x00\x00\x00" + b"\x00" * 32
    raise ValueError(name)


def _segment(marker: int, payload: bytes) -> bytes:
    return bytes((0xFF, marker)) + struct.pack(">H", len(payload) + 2) + payload


def jpeg_scans(n: int, *, stuffing: bool = False, restart: bool = False) -> bytes:
    """A synthetic progressive JPEG skeleton with `n` SOS segments (never a valid image)."""
    dqt = _segment(0xDB, b"\x00" + b"\x01" * 64)
    sof2 = _segment(0xC2, struct.pack(">BHHB", 8, 8, 8, 1) + b"\x01\x11\x00")
    dht = _segment(0xC4, b"\x00" + b"\x01" + b"\x00" * 15 + b"\x00")
    sos = _segment(0xDA, b"\x01\x01\x00\x00\x3f\x00")
    entropy = b"\x12\x34\x56"
    if stuffing:
        entropy += b"\xff\x00\x9a"
    if restart:
        entropy += b"\xff\xd0\xbc"
    entropy += b"\x78"
    return b"\xff\xd8" + dqt + sof2 + dht + (sos + entropy) * n + b"\xff\xd9"


def jpeg_with_exif_thumbnail() -> bytes:
    """A baseline JPEG whose APP1 (EXIF) payload carries a JPEG thumbnail, so `FF DA` bytes sit
    inside APP1."""
    thumb = encode(canvas(16, 16), "JPEG", quality=80)
    exif = Image.Exif()
    exif[0x0112] = 1
    return encode(canvas(64, 64), "JPEG", quality=90, exif=exif.tobytes() + thumb)


# --- rule and call helpers (the module under test is imported lazily) ---

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
IMAGE_ID = UUID("00000000-0000-4000-8000-000000000017")
_RULE_IDS = itertools.count(1)


def image_rule(
    check: str,
    *,
    marketplace: str = "amazon",
    slot: str = "main",
    severity: str = "error",
    pack: str = "core",
    enabled: bool = True,
    suggestion: str | None = None,
    version: int = 1,
    **params: Any,
) -> Rule:
    """One `images` rule row with a fresh id."""
    return Rule.model_validate(
        {
            "id": str(UUID(int=next(_RULE_IDS))),
            "marketplace": marketplace,
            "field": "images",
            "severity": severity,
            "version": version,
            "enabled": enabled,
            "rule_def": {
                "type": "image_requirement",
                "message": f"image {check}",
                "suggestion": suggestion,
                "pack": pack,
                "slot": slot,
                "check": check,
                "params": params,
            },
        }
    )


def run(
    data: bytes,
    rules: Sequence[Rule] = (),
    *,
    marketplace: str = "amazon",
    slot: str = "main",
    now: datetime = NOW,
) -> ImageReport:
    from app.services.image_checks import check_image

    return check_image(
        data,
        image_id=IMAGE_ID,
        marketplace=Marketplace(marketplace),
        slot=ImageSlot(slot),
        rules=rules,
        now=now,
    )


def names(report: ImageReport) -> list[str]:
    return [c.check for c in report.checks]


def codes(report: ImageReport) -> list[str]:
    return [i.code.value for i in report.issues]


def amazon_rules() -> list[Rule]:
    """The Amazon main seed shape (DATABASE.md image seed), thresholds as test data."""
    return [
        image_rule("max_bytes", slot="any", max_bytes=10 * 1024 * 1024),
        image_rule("format", slot="any", allowed_formats=["jpeg", "png", "tiff"]),
        image_rule("min_resolution", min_px=1000),
        image_rule("aspect_ratio", ratio_w=1, ratio_h=1),
        image_rule("background", rgb=(255, 255, 255)),
        image_rule("fill_ratio", severity="warning", min_fill=0.85),
    ]
