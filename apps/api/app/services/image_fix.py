"""Fix-only framing of a main photo (SI7, ADR-0030 proposed): zero tokens, deterministic Pillow.

`fix` turns a near-white, uniform background into pure white (255,255,255), trims to the product,
pads it to a white square where the product fills `FILL_TARGET` of the side, and sizes the square
to `OUT_MIN`..`OUT_MAX` px. It never changes the product's colours: no global brightness or levels
(PLAN D7). A source it cannot fix raises `FixRefused` with the reason the seller sees.

Every pixel pass is a Pillow C operation (point, difference, histogram, filters); only the
connectivity fill runs in Python, on a mask of at most `CONNECT_PX` px a side. No numpy (C:92).

Pure library: no database, settings, logger or I/O; run inside the killable image child.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final, Literal

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from app.services.image_checks import BAND_MIN_PX, BAND_PERCENT

__all__ = [
    "FILL_TARGET",
    "BorderProfile",
    "FixRefused",
    "border_profile",
    "fix",
    "foreground_bbox",
    "frame_square",
    "whiten_border",
]

NEAR_WHITE_MIN: Final = 235
UNIFORM_SPREAD: Final = 12
UNIFORM_SHARE: Final = 0.99
PURE_WHITE_MIN: Final = 254
FILL_TARGET: Final = 0.85
LEAK_MAX: Final = 0.92
OUT_MIN: Final = 1000
OUT_MAX: Final = 2000
MAX_UPSCALE: Final = 1.25
FG_THRESHOLD: Final = 8
# A source this thin has no product to frame (a 1 px image, a 4000 x 10 sliver).
MIN_SOURCE_SIDE: Final = 64
# The connectivity mask is filled at this size, then scaled back and intersected with the
# full-size colour mask, so the whitened pixels are exact and the Python fill stays small.
CONNECT_PX: Final = 512

WHITE: Final = (255, 255, 255)
Reason = Literal["too_small", "source_unusable"]


class FixRefused(Exception):  # noqa: N818 - CODE-PLAN name; a verdict, not a failure
    """The source cannot be fixed deterministically; `reason` is the candidate's failure reason."""

    def __init__(self, reason: Reason) -> None:
        super().__init__(reason)
        self.reason: Reason = reason


@dataclass(frozen=True, slots=True)
class BorderProfile:
    uniform: bool
    near_white: bool
    colour: tuple[int, int, int]


def _bands(img: Image.Image) -> list[Image.Image]:
    """The four edge bands (2 % of each side, at least 2 px), corners counted once."""
    w, h = img.size
    dx = min(w, max(BAND_MIN_PX, math.ceil(w * BAND_PERCENT / 100)))
    dy = min(h, max(BAND_MIN_PX, math.ceil(h * BAND_PERCENT / 100)))
    boxes = [(0, 0, w, dy), (0, max(dy, h - dy), w, h)]
    if h - 2 * dy > 0:
        boxes += [(0, dy, dx, h - dy), (max(dx, w - dx), dy, w, h - dy)]
    return [img.crop(box) for box in boxes if box[2] > box[0] and box[3] > box[1]]


def _median(hist: list[int]) -> int:
    total = sum(hist)
    half, seen = (total + 1) // 2, 0
    for value, count in enumerate(hist):
        seen += count
        if seen >= half:
            return value
    return 255


def _max_channel_diff(img: Image.Image, colour: tuple[int, int, int]) -> Image.Image:
    """An "L" image: per pixel, the largest channel distance from `colour`."""
    diff = ImageChops.difference(img, Image.new("RGB", img.size, colour))
    r, g, b = diff.split()
    return ImageChops.lighter(ImageChops.lighter(r, g), b)


def _rgb(img: Image.Image) -> Image.Image:
    return img if img.mode == "RGB" else img.convert("RGB")


def border_profile(img: Image.Image) -> BorderProfile:
    """The border's median colour, whether 99 % of its pixels sit within `UNIFORM_SPREAD` of it on
    every channel (uniform), and whether the median is near white (every channel >= 235)."""
    rgb = _rgb(img)
    bands = _bands(rgb)
    hist = [0] * 768
    for band in bands:
        hist = [a + b for a, b in zip(hist, band.histogram(), strict=True)]
    colour = (_median(hist[0:256]), _median(hist[256:512]), _median(hist[512:768]))
    within = total = 0
    for band in bands:
        counts = _max_channel_diff(band, colour).histogram()
        within += sum(counts[: UNIFORM_SPREAD + 1])
        total += band.width * band.height
    uniform = total > 0 and within / total >= UNIFORM_SHARE
    return BorderProfile(uniform=uniform, near_white=min(colour) >= NEAR_WHITE_MIN, colour=colour)


def _seeds(w: int, h: int) -> list[tuple[int, int]]:
    xs, ys = (0, w // 2, w - 1), (0, h // 2, h - 1)
    return [(x, y) for x in xs for y in ys if x in (0, w - 1) or y in (0, h - 1)]


def whiten_border(img: Image.Image, profile: BorderProfile | None = None) -> Image.Image:
    """Turn the background connected to the border (pixels within `UNIFORM_SPREAD` of the border
    colour) pure white. More than `LEAK_MAX` of the frame turning white means the product itself
    matched (a white product touching the border): `FixRefused("source_unusable")`."""
    rgb = _rgb(img)
    prof = profile or border_profile(rgb)
    near = _max_channel_diff(rgb, prof.colour).point(
        lambda v: 255 if v <= UNIFORM_SPREAD else 0, mode="L"
    )
    w, h = rgb.size
    scale = min(1.0, CONNECT_PX / max(w, h))
    sw, sh = max(1, round(w * scale)), max(1, round(h * scale))
    small = near.resize((sw, sh), Image.Resampling.BOX).point(
        lambda v: 255 if v >= 128 else 0, mode="L"
    )
    for seed in _seeds(sw, sh):
        if small.getpixel(seed) == 255:
            ImageDraw.floodfill(small, seed, 128, thresh=0)
    connected = small.point(lambda v: 255 if v == 128 else 0, mode="L")
    connected = connected.filter(ImageFilter.MaxFilter(3)).resize((w, h), Image.Resampling.NEAREST)
    mask = ImageChops.multiply(near, connected)
    hist = mask.histogram()
    if hist[255] / (w * h) > LEAK_MAX:
        raise FixRefused("source_unusable")
    return Image.composite(Image.new("RGB", (w, h), WHITE), rgb, mask)


def foreground_bbox(img: Image.Image) -> tuple[int, int, int, int] | None:
    """The bounding box of the pixels more than `FG_THRESHOLD` from pure white on any channel."""
    mask = _max_channel_diff(_rgb(img), WHITE).point(
        lambda v: 255 if v > FG_THRESHOLD else 0, mode="L"
    )
    return mask.getbbox()


def frame_square(img: Image.Image, *, max_upscale: float = MAX_UPSCALE) -> Image.Image:
    """Trim to the product, then centre it on a white square whose side makes the product's long
    side `FILL_TARGET` of it, sized to `OUT_MIN`..`OUT_MAX` px (LANCZOS). At scale 1.0 the
    product's pixels are copied unchanged. An upscale over `max_upscale` is `too_small`."""
    rgb = _rgb(img)
    if min(rgb.size) < MIN_SOURCE_SIDE:
        raise FixRefused("source_unusable")
    bbox = foreground_bbox(rgb)
    if bbox is None:
        raise FixRefused("source_unusable")
    product = rgb.crop(bbox)
    cw, ch = product.size
    side = math.ceil(max(cw, ch) / FILL_TARGET)
    target = min(OUT_MAX, max(OUT_MIN, side))
    scale = target / side
    if scale > max_upscale:
        raise FixRefused("too_small")
    if scale != 1.0:
        size = (max(1, round(cw * scale)), max(1, round(ch * scale)))
        product = product.resize(size, Image.Resampling.LANCZOS)
        cw, ch = product.size
    if min(cw, ch) < 2:
        raise FixRefused("source_unusable")
    canvas = Image.new("RGB", (target, target), WHITE)
    canvas.paste(product, ((target - cw) // 2, (target - ch) // 2))
    return canvas


def fix(img: Image.Image) -> Image.Image:
    """Whiten a uniform near-white border and frame; frame an already pure-white border as is;
    refuse any other border (that is the AI path's job)."""
    rgb = _rgb(img)
    if min(rgb.size) < MIN_SOURCE_SIDE:
        raise FixRefused("source_unusable")
    prof = border_profile(rgb)
    if prof.uniform and prof.near_white:
        return frame_square(whiten_border(rgb, prof))
    if min(prof.colour) >= PURE_WHITE_MIN:
        return frame_square(rgb)
    raise FixRefused("source_unusable")
