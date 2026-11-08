"""Fidelity of a model's white-background edit, and the composite that keeps the seller's pixels
(SI7, ADR-0030 proposed).

`judge` compares the edit with the seller's photo at `WORK_PX` and decides whether the product is
still the same product: same place, same size, same colours, nothing erased. `composite` then
keeps the seller's own pixels wherever the edit shows product, so the model only decides which
pixels are background (CP-D4).

Scores (all in 0..1):
- `agreement`: inside the edit's product mask, the share of pixels within `COLOUR_TOL` of the
  photo on every channel (a moved or recoloured product fails);
- `colour_distance`: the Bhattacharyya distance of the two 8x8x8 colour histograms under that
  mask (a recolour fails);
- `mask_iou`: the overlap of the two product masks, only when the photo's border is uniform
  (a scaled product fails); None on a busy border. The original is padded to a square with white
  (`PAD_COLOUR`), so its product mask is read inside the photo only (`photo_size`): the white
  padding is never product;
- `hole_share` (CP-D20): the share of product-coloured pixels the edit turned white near the
  product (an erased strap or sleeve fails on any border).

The thresholds are provisional, calibrated on synthetic images (P:§2 S5). One Python pass over at
most 65,536 pixels; no numpy (C:92). Pure library: no database, settings, logger or I/O.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

from PIL import Image, ImageChops, ImageFilter

from app.services.image_fix import BorderProfile

__all__ = [
    "PAD_COLOUR",
    "Fidelity",
    "composite",
    "decide",
    "judge",
    "pad_square",
]

WORK_PX: Final = 256
ASPECT_TOL: Final = 0.02
BLUR: Final = 1
CAND_WHITE: Final = 245
ORIG_DIST: Final = 24
COLOUR_TOL: Final = 32
AGREEMENT_MIN: Final = 0.90
HIST_MAX: Final = 0.15
IOU_MIN: Final = 0.80
HIST_BINS: Final = 8
HOLE_PAD: Final = 0.05
PALETTE_MIN: Final = 0.02
HOLE_MAX: Final = 0.10
# Agreement and colour are read on the candidate mask eroded by this filter size at the work size,
# so the blurred one-to-two pixel product edge (blended with the old background in the photo and
# with white in the edit) never counts as a change.
CORE_ERODE: Final = 7
# The model's input and the judged original are padded with white: a coloured bar at the edge is
# one more background the model may keep (live check 2026-11-08), and white padding composited
# back is white.
PAD_COLOUR: Final = (255, 255, 255)
# Work pixels next to the padding blend the photo with the white padding (bilinear plus the blur):
# they are left out of the original's product mask on the sides that have padding.
PHOTO_EDGE: Final = 2
# Composite mask: eroded one pixel, then feathered.
ERODE: Final = 3
FEATHER: Final = 1.5

_RGB = tuple[int, int, int]


@dataclass(frozen=True, slots=True)
class Fidelity:
    mask_iou: float | None
    colour_distance: float
    agreement: float
    hole_share: float
    passed: bool

    def as_json(self) -> dict[str, float | None]:
        return {
            "mask_iou": None if self.mask_iou is None else round(self.mask_iou, 4),
            "colour_distance": round(self.colour_distance, 4),
            "agreement": round(self.agreement, 4),
            "hole_share": round(self.hole_share, 4),
        }


def decide(
    *, mask_iou: float | None, colour_distance: float, agreement: float, hole_share: float
) -> bool:
    """The pass rule over the four scores."""
    return (
        agreement >= AGREEMENT_MIN
        and colour_distance <= HIST_MAX
        and (mask_iou is None or mask_iou >= IOU_MIN)
        and hole_share <= HOLE_MAX
    )


_FAILED: Final = Fidelity(
    mask_iou=None, colour_distance=1.0, agreement=0.0, hole_share=1.0, passed=False
)


def pad_square(img: Image.Image, colour: _RGB) -> Image.Image:
    """`img` centred on a square of `colour` (its long side), so the model sees a 1:1 frame."""
    rgb = img if img.mode == "RGB" else img.convert("RGB")
    w, h = rgb.size
    if w == h:
        return rgb
    side = max(w, h)
    out = Image.new("RGB", (side, side), colour)
    out.paste(rgb, ((side - w) // 2, (side - h) // 2))
    return out


def _work(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    rgb = img if img.mode == "RGB" else img.convert("RGB")
    return rgb.resize(size, Image.Resampling.BILINEAR).filter(ImageFilter.GaussianBlur(BLUR))


def _not_white_mask(img: Image.Image) -> Image.Image:
    """An "L" mask: 255 where any channel is below `CAND_WHITE`."""
    r, g, b = (c.point(lambda v: 255 if v < CAND_WHITE else 0, mode="L") for c in img.split())
    return ImageChops.lighter(ImageChops.lighter(r, g), b)


def _bin(p: _RGB) -> int:
    step = 256 // HIST_BINS
    return (p[0] // step) * HIST_BINS * HIST_BINS + (p[1] // step) * HIST_BINS + p[2] // step


def _bhattacharyya(a: list[int], b: list[int]) -> float:
    ta, tb = sum(a), sum(b)
    if ta == 0 or tb == 0:
        return 1.0
    coefficient = sum(math.sqrt(x * y) for x, y in zip(a, b, strict=True) if x and y) / math.sqrt(
        ta * tb
    )
    return math.sqrt(max(0.0, 1.0 - min(1.0, coefficient)))


def _near_white(p: _RGB) -> bool:
    return p[0] >= CAND_WHITE and p[1] >= CAND_WHITE and p[2] >= CAND_WHITE


def _photo_span(start: int, length: int, side: int, scale: float, work: int) -> tuple[int, int]:
    """The work-pixel range [lo, hi) of the photo along one axis of the padded square, without the
    `PHOTO_EDGE` pixels that blend into padding (a side flush with the frame has none)."""
    lo = 0 if start <= 0 else math.ceil(start * scale) + PHOTO_EDGE
    hi = work if start + length >= side else math.floor((start + length) * scale) - PHOTO_EDGE
    return lo, hi


def judge(
    original: Image.Image,
    candidate: Image.Image,
    border: BorderProfile,
    *,
    photo_size: tuple[int, int] | None = None,
) -> Fidelity:
    """Score the edit against the photo. A frame whose aspect differs by more than `ASPECT_TOL`
    fails at once, before any pixel work. `photo_size` is the seller's photo when `original` is
    it padded by `pad_square`: outside it the original is padding, never product."""
    ow, oh = original.size
    cw, ch = candidate.size
    if ow <= 0 or oh <= 0 or cw <= 0 or ch <= 0:
        return _FAILED
    if abs(ow / oh - cw / ch) > ASPECT_TOL * (ow / oh):
        return _FAILED
    scale = WORK_PX / max(ow, oh)
    size = (max(1, round(ow * scale)), max(1, round(oh * scale)))
    width = size[0]
    orig_img, cand_img = _work(original, size), _work(candidate, size)
    core = list(
        _not_white_mask(cand_img).filter(ImageFilter.MinFilter(CORE_ERODE)).get_flattened_data()
    )
    orig: list[_RGB] = list(orig_img.get_flattened_data())  # type: ignore[arg-type]
    cand: list[_RGB] = list(cand_img.get_flattened_data())  # type: ignore[arg-type]
    bg = border.colour
    x_lo, x_hi, y_lo, y_hi = 0, size[0], 0, size[1]
    if photo_size is not None:
        pw, ph = photo_size
        x_lo, x_hi = _photo_span((ow - pw) // 2, pw, ow, size[0] / ow, size[0])
        y_lo, y_hi = _photo_span((oh - ph) // 2, ph, oh, size[1] / oh, size[1])
    bins = HIST_BINS**3
    hist_o, hist_c = [0] * bins, [0] * bins
    cand_mask: list[bool] = []
    inside = cored = agree = orig_n = both = 0
    min_x = min_y = 1 << 30
    max_x = max_y = -1
    for i, (po, pc) in enumerate(zip(orig, cand, strict=True)):
        in_cand = not _near_white(pc)
        cand_mask.append(in_cand)
        x, y = i % width, i // width
        in_orig = (
            x_lo <= x < x_hi
            and y_lo <= y < y_hi
            and max(abs(po[0] - bg[0]), abs(po[1] - bg[1]), abs(po[2] - bg[2])) > ORIG_DIST
        )
        orig_n += in_orig
        if in_cand:
            inside += 1
            both += in_orig
            min_x, max_x = min(min_x, x), max(max_x, x)
            min_y, max_y = min(min_y, y), max(max_y, y)
        if core[i]:
            cored += 1
            if max(abs(po[0] - pc[0]), abs(po[1] - pc[1]), abs(po[2] - pc[2])) <= COLOUR_TOL:
                agree += 1
            hist_o[_bin(po)] += 1
            hist_c[_bin(pc)] += 1
    if inside == 0 or cored == 0:
        return _FAILED
    agreement = agree / cored
    colour_distance = _bhattacharyya(hist_o, hist_c)
    mask_iou: float | None = None
    if border.uniform:
        union = orig_n + inside - both
        mask_iou = both / union if union else 0.0
    # CP-D20: product-coloured pixels the edit made white, near the product
    palette = {b for b, n in enumerate(hist_o) if n >= PALETTE_MIN * cored}
    pad_x, pad_y = round(HOLE_PAD * size[0]), round(HOLE_PAD * size[1])
    x0, x1 = max(0, min_x - pad_x), min(size[0] - 1, max_x + pad_x)
    y0, y1 = max(0, min_y - pad_y), min(size[1] - 1, max_y + pad_y)
    suspects = 0
    for y in range(y0, y1 + 1):
        row = y * width
        for x in range(x0, x1 + 1):
            i = row + x
            if cand_mask[i]:
                continue
            po = orig[i]
            if not _near_white(po) and _bin(po) in palette:
                suspects += 1
    hole_share = suspects / inside
    return Fidelity(
        mask_iou=mask_iou,
        colour_distance=colour_distance,
        agreement=agreement,
        hole_share=hole_share,
        passed=decide(
            mask_iou=mask_iou,
            colour_distance=colour_distance,
            agreement=agreement,
            hole_share=hole_share,
        ),
    )


def composite(original_full: Image.Image, candidate: Image.Image) -> Image.Image:
    """The seller's pixels wherever the edit shows product; the edit's pixels (its white
    background) elsewhere. `original_full` is the square padded with `PAD_COLOUR` at the seller's
    resolution, so no coloured padding is ever pasted back; the edit is resized to it first
    (design §2.7)."""
    original = original_full if original_full.mode == "RGB" else original_full.convert("RGB")
    output = candidate if candidate.mode == "RGB" else candidate.convert("RGB")
    if output.size != original.size:
        output = output.resize(original.size, Image.Resampling.LANCZOS)
    mask = _not_white_mask(output)
    mask = mask.filter(ImageFilter.MinFilter(ERODE)).filter(ImageFilter.GaussianBlur(FEATHER))
    return Image.composite(original, output, mask)
