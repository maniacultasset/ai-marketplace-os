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


# --- SI1 T15: metadata-laden and hostile-size fixtures for the derivatives ---

XMP_PACKET = (
    b'<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>'
    b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf='
    b'"http://www.w3.org/1999/02/22-rdf-syntax-ns#"/></x:xmpmeta><?xpacket end="w"?>'
)


def jpeg_with_gps_orientation6(w: int = 40, h: int = 20) -> bytes:
    """A JPEG with EXIF (Make, orientation 6, a GPS block), an XMP APP1 packet and a COM marker.
    Orientation 6 means the stored w x h shows rotated to h x w."""
    exif = Image.Exif()
    exif[0x010F] = "AcmeCam"  # Make
    exif[0x0112] = 6  # Orientation: rotate 90 CW to view
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"
    gps[2] = (12.0, 58.0, 0.0)
    gps[3] = "E"
    gps[4] = (77.0, 35.0, 0.0)
    data = encode(canvas(w, h), "JPEG", quality=90, exif=exif.tobytes(), comment=b"shot by acme")
    xmp = _segment(0xE1, b"http://ns.adobe.com/xap/1.0/\x00" + XMP_PACKET)
    return data[:2] + xmp + data[2:]


def png_with_text_chunk() -> bytes:
    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", "secret location 12.97N 77.59E")
    return encode(canvas(32, 32), "PNG", pnginfo=info)


def webp_rgba(w: int = 32, h: int = 32) -> bytes:
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    img.paste((200, 20, 20, 255), (8, 8, w - 8, h - 8))
    return encode(img, "WEBP", lossless=True)


def noisy_png_rgba(w: int = 6000, h: int = 6000, seed: int = 7) -> bytes:
    """Random RGBA pixels: a PNG far over 10 MiB once re-encoded (built in memory, not committed)."""
    import random

    rng = random.Random(seed)  # noqa: S311 - test pixels, not secrets
    img = Image.frombytes("RGBA", (w, h), rng.randbytes(w * h * 4))
    return encode(img, "PNG", compress_level=1)


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


# ---- SI14 T22: the photo QA fixtures (text, watermarks, hard negatives) ----


def _font(size: int) -> Any:
    from PIL import ImageFont

    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # an older Pillow: the fixed bitmap font
        return ImageFont.load_default()


def qa_product(side: int = 1024, *, product: RGB = (40, 60, 90), fill: float = 0.55) -> Image.Image:
    return canvas(side, side, product=product, fill=fill)


def with_text(img: Image.Image, lines: Sequence[str] = ("SALE 50% OFF", "FREE SHIPPING")) -> bytes:
    from PIL import ImageDraw

    out = img.copy()
    draw = ImageDraw.Draw(out)
    font = _font(max(12, out.width // 18))
    y = out.height // 30
    for line in lines:
        draw.text((out.width // 20, y), line, fill=(20, 20, 20), font=font)
        y += out.width // 12
    return encode(out, "JPEG", quality=92)


def tiled_mark(img: Image.Image, period: float = 0.25) -> bytes:
    from PIL import ImageDraw

    base = img.convert("RGBA")
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    step = int(base.width * period)
    font = _font(max(12, step // 5))
    for x in range(0, base.width, step):
        for y in range(0, base.height, step):
            draw.text((x + step // 6, y + step // 3), "STUDIO", fill=(120, 120, 120, 70), font=font)
    return encode(Image.alpha_composite(base, layer).convert("RGB"), "JPEG", quality=92)


def corner_logo(img: Image.Image) -> bytes:
    from PIL import ImageDraw

    out = img.copy()
    draw = ImageDraw.Draw(out)
    side = out.width // 9
    x0, y0 = out.width - side - out.width // 40, out.height - side - out.height // 40
    for i in range(0, side, max(2, side // 12)):
        draw.line((x0 + i, y0, x0 + i, y0 + side), fill=(30, 30, 30), width=1)
        draw.line((x0, y0 + i, x0 + side, y0 + i), fill=(200, 30, 30), width=1)
    return encode(out, "JPEG", quality=92)


def striped_shirt(side: int = 1024) -> bytes:
    from PIL import ImageDraw

    img = qa_product(side, product=(240, 240, 240), fill=0.6)
    draw = ImageDraw.Draw(img)
    left = top = int(side * 0.2)
    right = bottom = int(side * 0.8)
    stripe = max(4, side // 40)
    for y in range(top, bottom, stripe * 2):
        draw.rectangle((left, y, right, min(bottom, y + stripe)), fill=(20, 40, 120))
    return encode(img, "JPEG", quality=92)


def checked_pattern(side: int = 1024) -> bytes:
    from PIL import ImageDraw

    img = qa_product(side, product=(230, 230, 230), fill=0.6)
    draw = ImageDraw.Draw(img)
    left = top = int(side * 0.2)
    right = bottom = int(side * 0.8)
    cell = max(4, side // 32)
    for i, y in enumerate(range(top, bottom, cell)):
        for j, x in enumerate(range(left, right, cell)):
            if (i + j) % 2 == 0:
                draw.rectangle(
                    (x, y, min(right, x + cell), min(bottom, y + cell)), fill=(150, 20, 20)
                )
    return encode(img, "JPEG", quality=92)


def garment_silhouette(
    w: int = 1200, h: int = 1600, *, background: RGB = (150, 140, 120), product: RGB = (30, 70, 180)
) -> bytes:
    """A flat kurta outline on a grey background: the prod Chrome pass photo of 12 Nov (B6), whose
    slanted shoulders, sleeves and sides were read as two lines of text."""
    from PIL import ImageDraw

    img = Image.new("RGB", (w, h), background)
    points = [
        (450, 200),
        (750, 200),
        (850, 350),
        (950, 700),
        (870, 730),
        (800, 525),
        (820, 1450),
        (380, 1450),
        (400, 525),
        (330, 730),
        (250, 700),
        (350, 350),
    ]
    ImageDraw.Draw(img).polygon([(x * w / 1200, y * h / 1600) for x, y in points], fill=product)
    return encode(img, "JPEG", quality=90)


def clean_products() -> dict[str, bytes]:
    return {
        "garment_silhouette": garment_silhouette(),
        "garment_square": garment_silhouette(1024, 1024),
        "plain": encode(qa_product(), "JPEG", quality=92),
        "large": encode(qa_product(fill=0.9), "JPEG", quality=92),
        "light": encode(qa_product(product=(200, 180, 160)), "JPEG", quality=92),
        "wide": encode(canvas(1200, 800, fill=0.7), "JPEG", quality=92),
        "striped_shirt": striped_shirt(),
        "checked": checked_pattern(),
    }
