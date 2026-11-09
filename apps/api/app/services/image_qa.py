"""Deterministic photo QA measurements (SI14, AI-43; T21-T22). Pure Pillow, no I/O, no model.

Each runs on a small copy of the decoded image (`qa_copy`: at most `QA_SIDE` px on the long side,
reduced in C by `Image.reduce`), so the cost is one pass over a few thousand pixels whatever the
upload size. numpy is banned (RULES §4); only Pillow's C operations (`ImageChops`, `ImageFilter`,
`ImageStat`, `point` tables) touch pixels, never a per-pixel Python loop.

- `fill_ratio(img)`: the background colour is the median of the four corner patches; the product
  is every pixel more than `FILL_TOLERANCE` off it on some channel; the fill is the product's
  bounding box over the image area (0.0 for an all-background image).

The results feed warnings only (`image_checks._WARNING_ONLY`): a QA check never blocks a listing.
The thresholds are tuned on the generated fixtures; the precision gate is the 100 labelled images
(D12, `docs/runbooks/V1-1-SWITCHES.md`).
"""

from __future__ import annotations

from typing import Final

from PIL import Image, ImageChops, ImageStat

__all__ = ["FILL_TOLERANCE", "QA_SIDE", "background_colour", "fill_ratio", "qa_copy"]

QA_SIDE: Final = 256
FILL_TOLERANCE: Final = 12
CORNER_PERCENT: Final = 5


def qa_copy(img: Image.Image) -> Image.Image:
    """An RGB copy at most `QA_SIDE` px on the long side (integer `reduce`, then a resize)."""
    w, h = img.size
    factor = max(1, max(w, h) // QA_SIDE)
    small = img.reduce(factor) if factor > 1 else img.copy()
    if small.mode not in ("RGB", "L"):
        if small.mode in ("I", "F") or small.mode.startswith("I;16"):
            small = small.convert("F").point(lambda v: v / 257).convert("L")
        small = small.convert("RGBA")
        backdrop = Image.new("RGBA", small.size, (255, 255, 255, 255))
        small = Image.alpha_composite(backdrop, small)
    small = small.convert("RGB")
    if max(small.size) > QA_SIDE:
        small.thumbnail((QA_SIDE, QA_SIDE))
    return small


def background_colour(small: Image.Image) -> tuple[int, int, int]:
    """The per-channel median of the four corner patches."""
    w, h = small.size
    cw, ch = max(1, w * CORNER_PERCENT // 100), max(1, h * CORNER_PERCENT // 100)
    strip = Image.new("RGB", (cw * 4, ch))
    for i, box in enumerate(
        ((0, 0, cw, ch), (w - cw, 0, w, ch), (0, h - ch, cw, h), (w - cw, h - ch, w, h))
    ):
        strip.paste(small.crop(box), (i * cw, 0))
    r, g, b = ImageStat.Stat(strip).median
    return int(r), int(g), int(b)


def _off_mask(small: Image.Image, colour: tuple[int, int, int]) -> Image.Image:
    """`L` mask: 255 where some channel is more than `FILL_TOLERANCE` off `colour`."""
    lut: list[int] = []
    for target in colour:
        lut += [255 if abs(v - target) > FILL_TOLERANCE else 0 for v in range(256)]
    r, g, b = small.point(lut).split()
    return ImageChops.lighter(ImageChops.lighter(r, g), b)


def fill_ratio(img: Image.Image) -> float:
    small = qa_copy(img)
    box = _off_mask(small, background_colour(small)).getbbox()
    if box is None:
        return 0.0
    w, h = small.size
    return ((box[2] - box[0]) * (box[3] - box[1])) / float(w * h)
