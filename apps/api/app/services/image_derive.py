"""Image derivatives (SI1 T15; PLAN D5, CP17): the `clean` copy and the `vision` copy.

Pure: Pillow and the stdlib only, imported only by `image_derive_child` (import contract
`image-check-child-is-pure`). `derive(data)` never raises:

- `clean`: EXIF orientation applied, then saved with no EXIF, no XMP and no comment, the ICC
  profile kept. JPEG and PNG keep their format (PNG text chunks are dropped); anything else
  (WebP, TIFF) and any image with alpha that is not a PNG becomes a JPEG on white. It is at most
  `MAX_IMAGE_BYTES`: an oversize PNG is re-encoded as JPEG q92, then a JPEG is downscaled by 0.75
  a step (at most 4 steps); still over → None.
- `vision`: RGB JPEG, long side at most 1024 (LANCZOS), quality 85 lowered by 10 down to 45 until
  its base64 fits `MAX_CONTEXT_IMAGE_B64_CHARS` (it goes to the model provider).

A decode failure, a decompression bomb, a `MemoryError` or any other failure is None.
"""

from __future__ import annotations

import base64
import io
import math
from dataclasses import dataclass
from typing import Final

from mkos_schemas.jobs import MAX_CONTEXT_IMAGE_B64_CHARS, MAX_CONTEXT_IMAGE_PX
from PIL import Image, ImageOps

from app.services.image_checks import DECODE_FORMATS, MAX_PIXELS, MAX_SIDE

__all__ = ["DERIVE_FORMATS", "MAX_IMAGE_BYTES", "Derived", "derive"]

MAX_IMAGE_BYTES: Final = 10_485_760
DERIVE_FORMATS: Final = (*DECODE_FORMATS, "WEBP")
CLEAN_JPEG_QUALITY: Final = 92
DOWNSCALE: Final = 0.75
MAX_DOWNSCALE_STEPS: Final = 4
VISION_QUALITIES: Final = (85, 75, 65, 55, 45)
WHITE: Final = (255, 255, 255)


@dataclass(frozen=True, slots=True)
class Derived:
    clean: bytes
    clean_mime: str
    vision: bytes
    vision_w: int
    vision_h: int


def _rgb_on_white(img: Image.Image) -> Image.Image:
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        base = Image.new("RGB", rgba.size, WHITE)
        base.paste(rgba, mask=rgba.getchannel("A"))
        return base
    return img.convert("RGB")


def _jpeg(img: Image.Image, quality: int, icc: bytes | None) -> bytes:
    out = io.BytesIO()
    extra = {"icc_profile": icc} if icc else {}
    _rgb_on_white(img).save(out, format="JPEG", quality=quality, optimize=True, **extra)
    return out.getvalue()


def _png(img: Image.Image, icc: bytes | None) -> bytes:
    out = io.BytesIO()
    extra = {"icc_profile": icc} if icc else {}
    keep = img if img.mode in ("RGB", "RGBA", "L", "LA", "P") else img.convert("RGBA")
    # a fresh image carries no `info`, so no text chunk, EXIF or XMP is written
    keep.save(out, format="PNG", optimize=True, **extra)
    return out.getvalue()


def _fresh(img: Image.Image) -> Image.Image:
    """A copy of the pixels with an empty `info`, so no EXIF, XMP, comment or text chunk of the
    source can reach an encoder's defaults. A `transparency` key (a PNG tRNS chunk) is first
    turned into a real alpha channel, so clearing `info` never makes the background opaque."""
    if "transparency" in img.info:
        img = img.convert("LA" if img.mode in ("L", "LA") else "RGBA")
    copy = img.copy()
    copy.info = {}
    return copy


def _clean(img: Image.Image, fmt: str, icc: bytes | None) -> tuple[bytes, str] | None:
    if fmt == "PNG":
        data = _png(img, icc)
        if len(data) <= MAX_IMAGE_BYTES:
            return data, "image/png"
    data = _jpeg(img, CLEAN_JPEG_QUALITY, icc)
    step = img
    for _ in range(MAX_DOWNSCALE_STEPS):
        if len(data) <= MAX_IMAGE_BYTES:
            return data, "image/jpeg"
        w, h = step.size
        size = (max(1, math.floor(w * DOWNSCALE)), max(1, math.floor(h * DOWNSCALE)))
        step = step.resize(size, Image.Resampling.LANCZOS)
        data = _jpeg(step, CLEAN_JPEG_QUALITY, icc)
    return (data, "image/jpeg") if len(data) <= MAX_IMAGE_BYTES else None


def _vision(img: Image.Image) -> tuple[bytes, int, int] | None:
    small = _rgb_on_white(img)
    small.thumbnail((MAX_CONTEXT_IMAGE_PX, MAX_CONTEXT_IMAGE_PX), Image.Resampling.LANCZOS)
    for quality in VISION_QUALITIES:
        out = io.BytesIO()
        small.save(out, format="JPEG", quality=quality, optimize=True)
        data = out.getvalue()
        if len(base64.b64encode(data)) <= MAX_CONTEXT_IMAGE_B64_CHARS:
            return data, small.size[0], small.size[1]
    return None


def _derive(data: bytes) -> Derived | None:
    with Image.open(io.BytesIO(data), formats=list(DERIVE_FORMATS)) as probe:
        width, height = probe.size
        if width < 1 or height < 1 or width * height > MAX_PIXELS or max(width, height) > MAX_SIDE:
            return None
        fmt = probe.format or ""
        icc = probe.info.get("icc_profile")
        probe.load()
        upright = ImageOps.exif_transpose(probe)
    pixels = _fresh(upright)
    clean = _clean(pixels, fmt if fmt in ("JPEG", "PNG") else "JPEG", icc)
    if clean is None:
        return None
    vision = _vision(pixels)
    if vision is None:
        return None
    return Derived(
        clean=clean[0],
        clean_mime=clean[1],
        vision=vision[0],
        vision_w=vision[1],
        vision_h=vision[2],
    )


def derive(data: bytes) -> Derived | None:
    """The two derivatives of `data`, or None (never raises; see the module docstring)."""
    try:
        return _derive(data)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        return None
