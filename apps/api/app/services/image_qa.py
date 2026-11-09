"""Deterministic photo QA measurements (SI14, AI-43; T21-T22). Pure Pillow, no I/O, no model.

Each runs on a small copy of the decoded image (`qa_copy`: at most `QA_SIDE` px on the long side,
reduced in C by `Image.reduce`), so the cost is one pass over a few thousand pixels whatever the
upload size. numpy is banned (RULES §4); only Pillow's C operations (`ImageChops`, `ImageFilter`,
`ImageStat`, `point` tables) touch pixels, never a per-pixel Python loop.

- `fill_ratio(img)`: the background colour is the median of the four corner patches; the product
  is every pixel more than `FILL_TOLERANCE` off it on some channel; the fill is the product's
  bounding box over the image area (0.0 for an all-background image).
- `text_overlay(img)`: the edge map (`FIND_EDGES`, threshold `EDGE_THRESHOLD`) is cut into row
  bands (consecutive rows with edges); a band `TEXT_BAND_MIN`..`TEXT_BAND_MAX` of the height is a
  line of text when its column profile holds at least `TEXT_RUNS_PER_LINE` narrow runs (glyphs,
  or words once the copy merges them). Text is `TEXT_MIN_LINES` lines with `TEXT_MIN_RUNS` runs,
  or one line of `TEXT_ONE_LINE_RUNS`. A product's outline makes tall bands or long runs, so it
  never counts.
- `watermark(img)`: (a) a corner patch (15 % x 15 %) whose edge density is over
  `CORNER_RATIO` times the median of the other corners, or (b) a repeating low-contrast tile:
  scattered detail (edges and low-contrast high-pass, no long straight line) in every corner
  patch, where a clean product shot is empty.

The results feed warnings only (`image_checks._WARNING_ONLY`): a QA check never blocks a listing.
The thresholds are tuned on the generated fixtures; the precision gate is the 100 labelled images
(D12, `docs/runbooks/V1-1-SWITCHES.md`).
"""

from __future__ import annotations

import statistics
import struct
from typing import Final

from PIL import Image, ImageChops, ImageFilter, ImageStat

__all__ = [
    "FILL_TOLERANCE",
    "QA_SIDE",
    "background_colour",
    "fill_ratio",
    "qa_copy",
    "text_overlay",
    "watermark",
]

QA_SIDE: Final = 256
FILL_TOLERANCE: Final = 12
CORNER_PERCENT: Final = 5
# tuned on the fixtures (tests/image_fixtures.py); the precision gate is the 100 labelled images
EDGE_THRESHOLD: Final = 96
TEXT_BAND_MIN: Final = 0.005
TEXT_BAND_MAX: Final = 0.08
TEXT_RUN_MAX: Final = 0.35
TEXT_RUNS_PER_LINE: Final = 2
TEXT_MIN_LINES: Final = 2
TEXT_MIN_RUNS: Final = 5
TEXT_ONE_LINE_RUNS: Final = 6
CORNER_SIDE: Final = 0.15
CORNER_RATIO: Final = 4.0
CORNER_MIN_DENSITY: Final = 0.02
TILE_LOW: Final = 6
TILE_HIGH: Final = 48
TILE_MIN_SHARE: Final = 0.03
FRAME: Final = 2
LINE_GAP: Final = 3
LINE_NEIGHBOUR: Final = 0.15


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


def _edges(small: Image.Image) -> Image.Image:
    """`L` mask: 255 on a high-contrast edge. `FIND_EDGES` marks the image border itself, so a
    `FRAME` px frame is cleared."""
    gray = small.convert("L").filter(ImageFilter.FIND_EDGES)
    mask = gray.point([255 if v >= EDGE_THRESHOLD else 0 for v in range(256)])
    w, h = mask.size
    if w > 2 * FRAME and h > 2 * FRAME:
        inner = mask.crop((FRAME, FRAME, w - FRAME, h - FRAME))
        mask = Image.new("L", (w, h), 0)
        mask.paste(inner, (FRAME, FRAME))
    return mask


def _profile(mask: Image.Image, horizontal: bool) -> list[float]:
    """The share of edge pixels per column (`horizontal`) or per row, in C (a BOX resize)."""
    w, h = mask.size
    size = (w, 1) if horizontal else (1, h)
    line = mask.convert("F").resize(size, Image.Resampling.BOX)
    count = w if horizontal else h
    return [v / 255 for v in struct.unpack(f"={count}f", line.tobytes())]


def _runs(values: list[float], floor: float = 0.0) -> list[tuple[int, int]]:
    """(start, length) of each run of values over `floor`."""
    out: list[tuple[int, int]] = []
    start: int | None = None
    for i, value in enumerate([*values, 0.0]):
        if value > floor and start is None:
            start = i
        elif value <= floor and start is not None:
            out.append((start, i - start))
            start = None
    return out


def text_overlay(img: Image.Image) -> bool:
    small = qa_copy(img)
    mask = _edges(small)
    w, h = mask.size
    lines = runs = 0
    widest_line = 0
    for top, height in _runs(_profile(mask, horizontal=False)):
        if not TEXT_BAND_MIN * h <= height <= TEXT_BAND_MAX * h:
            continue
        band = mask.crop((0, top, w, top + height))
        narrow = [r for r in _runs(_profile(band, horizontal=True)) if r[1] <= TEXT_RUN_MAX * w]
        if len(narrow) >= TEXT_RUNS_PER_LINE:
            lines += 1
            runs += len(narrow)
            widest_line = max(widest_line, len(narrow))
    return (lines >= TEXT_MIN_LINES and runs >= TEXT_MIN_RUNS) or widest_line >= TEXT_ONE_LINE_RUNS


def _density(mask: Image.Image, box: tuple[int, int, int, int]) -> float:
    crop = mask.crop(box)
    return crop.histogram()[255] / float(max(1, crop.width * crop.height))


def _corner_mark(mask: Image.Image) -> bool:
    w, h = mask.size
    cw, ch = max(1, int(w * CORNER_SIDE)), max(1, int(h * CORNER_SIDE))
    boxes = ((0, 0, cw, ch), (w - cw, 0, w, ch), (0, h - ch, cw, h), (w - cw, h - ch, w, h))
    densities = [_density(mask, box) for box in boxes]
    for i, density in enumerate(densities):
        others = densities[:i] + densities[i + 1 :]
        if density >= CORNER_MIN_DENSITY and density > CORNER_RATIO * statistics.median(others):
            return True
    return False


def _isolated_line(profile: list[float]) -> bool:
    """A row (or column) over half lit with its neighbours `LINE_GAP` away nearly dark: a thin
    straight line, such as a product boundary (text rows have lit neighbours)."""
    for i, share in enumerate(profile):
        if share < 0.5:
            continue
        before = profile[i - LINE_GAP] if i >= LINE_GAP else 0.0
        after = profile[i + LINE_GAP] if i + LINE_GAP < len(profile) else 0.0
        if max(before, after) < LINE_NEIGHBOUR:
            return True
    return False


def _scattered(mask: Image.Image, box: tuple[int, int, int, int]) -> bool:
    """Detail over `TILE_MIN_SHARE` of the patch, and no thin straight line across it (a
    product's boundary reaching into the corner, not a mark)."""
    if _density(mask, box) < TILE_MIN_SHARE:
        return False
    crop = mask.crop(box)
    return not (
        _isolated_line(_profile(crop, horizontal=True))
        or _isolated_line(_profile(crop, horizontal=False))
    )


def _tile(small: Image.Image) -> bool:
    """A mark repeated over the photo reaches every corner: each corner patch holds scattered
    detail (the high-contrast edges, plus the low-contrast high-pass in `TILE_LOW`..`TILE_HIGH`).
    A clean product shot has empty corners; a product reaching into a corner shows a straight
    boundary there."""
    gray = small.convert("L")
    highpass = ImageChops.difference(gray, gray.filter(ImageFilter.GaussianBlur(2)))
    low = highpass.point([255 if TILE_LOW <= v <= TILE_HIGH else 0 for v in range(256)])
    detail = ImageChops.lighter(low, _edges(small))
    w, h = detail.size
    cw, ch = max(1, int(w * CORNER_SIDE)), max(1, int(h * CORNER_SIDE))
    boxes = ((0, 0, cw, ch), (w - cw, 0, w, ch), (0, h - ch, cw, h), (w - cw, h - ch, w, h))
    return all(_scattered(detail, box) for box in boxes)


def watermark(img: Image.Image) -> bool:
    small = qa_copy(img)
    return _corner_mark(_edges(small)) or _tile(small)
