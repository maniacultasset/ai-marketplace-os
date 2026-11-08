"""Fidelity of a model's white-background edit, and the composite that keeps the seller's pixels
(SI7, ADR-0030 proposed).

The model's output decides one thing only: which pixels are background (CP-D4). An image model
re-scales, shifts and redraws the subject a little, so `align` first registers the output onto
the seller's photo: a scale and a shift (the two axes' scales within `ASPECT_MAX` of each other)
found coarse to fine. A redrawn pose has no such fit and fails `agreement`.
`composite` then builds the final image from the seller's own pixels under the registered product
mask, on pure white. The product sold is the seller's photo by construction: the model's pixels
never reach the output.

`judge` registers the output and decides whether its mask can be trusted, at `WORK_PX`. Scores
(all in 0..1):
- `agreement`: inside the registered product mask (eroded, so a redrawn edge never counts), the
  share of pixels within `COLOUR_TOL` of the photo on every channel. A product the registration
  cannot place (a redrawn pose), a recoloured product, or a mask over the old background fails;
- `colour_distance`: the Bhattacharyya distance of the two 8x8x8 colour histograms under that
  mask, the smaller of two bin grids half a bin apart, so a flat colour on a bin edge does not
  split (a recolour fails on both);
- `mask_iou`: the overlap of the registered mask with the photo's own product mask, only when the
  photo's border is uniform; None on a busy border. The original is padded to a square with white
  (`PAD_COLOUR`), so its product mask is read inside the photo only (`photo_size`);
- `hole_share` (CP-D20): photo pixels clear of the mask's edge by `HOLE_RING` whose colour is
  more typical of the product than of the photo's background and that join the product through
  such pixels, per product pixel, capped at 1 (an erased strap or sleeve, or a
  product cut off, fails on any border; a busy grey backdrop the model removed, or a dark plant
  apart from a dark product, does not).

The registration minimises, over the photo, the capped colour difference under the registered
mask plus a cost for every product-like pixel the mask leaves out (`_likelihood`: the model's
product colours against the photo's far background), so it neither shrinks onto the best patch
nor stretches over the backdrop.

The thresholds are provisional, calibrated on synthetic images and live Gemini edits of three
photos (2026-11-08; P:§2 S5).
Pixel passes are Pillow C operations; the Python loops run over at most `WORK_PX` squared pixels.
No numpy (C:92). Pure library: no database, settings, logger or I/O.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

from PIL import Image, ImageChops, ImageFilter, ImageStat

from app.services.image_fix import BorderProfile

__all__ = [
    "IDENTITY",
    "PAD_COLOUR",
    "Alignment",
    "Fidelity",
    "align",
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
PALETTE_MIN: Final = 0.02
HOLE_MAX: Final = 0.10
# Agreement and colour are read on the registered mask eroded by this filter size at the work
# size, so the blurred one-to-two pixel product edge (blended with the old background in the photo
# and redrawn by the model) never counts as a change.
CORE_ERODE: Final = 7
# A photo pixel this close to the registered mask (a MaxFilter size at the work size) is edge, not
# a hole: the model redraws edges and the registration is good to a pixel or two.
HOLE_RING: Final = 3
# The model's input and the judged original are padded with white: a coloured bar at the edge is
# one more background the model may keep (live check 2026-11-08), and white padding is white.
PAD_COLOUR: Final = (255, 255, 255)
# Work pixels next to the padding blend the photo with the white padding (bilinear plus the blur):
# they are left out of the original's product mask on the sides that have padding.
PHOTO_EDGE: Final = 2

# Registration: output = (sx * (x - 1/2) + 1/2 + dx, sy * (y - 1/2) + 1/2 + dy) for a point
# (x, y) of the original's square, in units of the side. Each scale in SCALE_MIN..SCALE_MAX, the
# two within ASPECT_MAX of each other (a stretch could cover an erased sleeve), each shift at most
# SHIFT_MAX.
SCALE_MIN: Final = 0.80
SCALE_MAX: Final = 1.25
ASPECT_MAX: Final = 0.01
SHIFT_MAX: Final = 0.25
COARSE_PX: Final = 32
FIT_PX: Final = 128
SCALE_STEP: Final = 1.02
# The descent's first step, in pixels of the level it runs at.
STEP_START: Final = 4
# The last descent runs at this size, so the composite's mask is placed to a pixel or two at the
# seller's resolution.
FINE_PX: Final = 512
# A tie-break toward the identity, per unit of scale or shift: far below the cost of a pixel of
# misfit, so it only settles a flat product that fits equally well a little larger or smaller.
TIE: Final = 2.0
# Each pixel's colour difference counts at most this much (0..255) in the registration cost, and
# so does each product-like pixel the mask leaves out: a redrawn strand of hair cannot outweigh
# the rest.
DIFF_CAP: Final = 64
_CAP_LUT: Final = [min(v, DIFF_CAP) for v in range(256)] * 3
# The far background the likelihood learns from: photo pixels outside the model's product mask
# grown by this share of the side.
BG_GAP: Final = 0.06

# Composite mask at the seller's resolution: eroded one pixel, then feathered.
ERODE: Final = 3
FEATHER: Final = 1.5
WHITE: Final = (255, 255, 255)

_RGB = tuple[int, int, int]


@dataclass(frozen=True, slots=True)
class Alignment:
    """Where a point (x, y) of the original's square lands in the model's output, in units of the
    side: scaled about the centre, then shifted, so a scale step does not move the subject."""

    sx: float
    sy: float
    dx: float
    dy: float

    def affine(self, out_side: int, in_size: tuple[int, int]) -> tuple[float, ...]:
        """Pillow AFFINE data sampling an `in_size` output for an `out_side` square of the
        original."""
        w, h = in_size
        cx, cy = 0.5 - self.sx / 2 + self.dx, 0.5 - self.sy / 2 + self.dy
        return (self.sx * w / out_side, 0.0, cx * w, 0.0, self.sy * h / out_side, cy * h)


IDENTITY: Final = Alignment(1.0, 1.0, 0.0, 0.0)


@dataclass(frozen=True, slots=True)
class Fidelity:
    mask_iou: float | None
    colour_distance: float
    agreement: float
    hole_share: float
    passed: bool
    alignment: Alignment = IDENTITY

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
    rgb = _rgb(img)
    w, h = rgb.size
    if w == h:
        return rgb
    side = max(w, h)
    out = Image.new("RGB", (side, side), colour)
    out.paste(rgb, ((side - w) // 2, (side - h) // 2))
    return out


def _rgb(img: Image.Image) -> Image.Image:
    return img if img.mode == "RGB" else img.convert("RGB")


def _work(img: Image.Image, side: int) -> Image.Image:
    small = _rgb(img).resize((side, side), Image.Resampling.BILINEAR)
    return small.filter(ImageFilter.GaussianBlur(BLUR))


def _not_white_mask(img: Image.Image) -> Image.Image:
    """An "L" mask: 255 where any channel is below `CAND_WHITE`."""
    r, g, b = (c.point(lambda v: 255 if v < CAND_WHITE else 0, mode="L") for c in img.split())
    return ImageChops.lighter(ImageChops.lighter(r, g), b)


def _warp(img: Image.Image, al: Alignment, side: int, fill: _RGB | int) -> Image.Image:
    """`img` (the model's output or its mask) resampled into the original's square frame."""
    return img.transform(
        (side, side),
        Image.Transform.AFFINE,
        al.affine(side, img.size),
        Image.Resampling.BILINEAR,
        fillcolor=fill,
    )


def _bin(p: _RGB, offset: int = 0) -> int:
    """The 8x8x8 histogram bin of `p`; `offset` shifts the bin edges (a shifted grid has one more
    bin per channel, so no colour falls off it)."""
    step = 256 // HIST_BINS
    side = HIST_BINS + 1
    r, g, b = ((c + offset) // step for c in p)
    return (r * side + g) * side + b


def _photo_box(
    original_side: int, photo_size: tuple[int, int] | None, work: int
) -> tuple[int, int, int, int]:
    """The work-pixel box [x_lo, x_hi) x [y_lo, y_hi) of the seller's photo inside the padding."""
    if photo_size is None:
        return 0, work, 0, work
    pw, ph = photo_size
    scale = work / original_side
    x_lo, x_hi = _photo_span((original_side - pw) // 2, pw, original_side, scale, work)
    y_lo, y_hi = _photo_span((original_side - ph) // 2, ph, original_side, scale, work)
    return x_lo, x_hi, y_lo, y_hi


def _photo_span(start: int, length: int, side: int, scale: float, work: int) -> tuple[int, int]:
    """The work-pixel range [lo, hi) of the photo along one axis of the padded square, without the
    `PHOTO_EDGE` pixels that blend into padding (a side flush with the frame has none)."""
    lo = 0 if start <= 0 else math.ceil(start * scale) + PHOTO_EDGE
    hi = work if start + length >= side else math.floor((start + length) * scale) - PHOTO_EDGE
    return lo, hi


def _likelihood(
    orig: Image.Image, cand: Image.Image, al: Alignment, box: tuple[int, int, int, int]
) -> Image.Image:
    """An "L" map over the original (same size as `orig`): 255 x P(product | colour), from the
    model's product colours against the photo's background far from the registered product."""
    n = orig.width
    cand_mask = _not_white_mask(cand)
    core = list(cand_mask.filter(ImageFilter.MinFilter(3)).get_flattened_data())
    bins = (HIST_BINS + 1) ** 3
    fg, bg = [0] * bins, [0] * bins
    for flag, p in zip(core, cand.get_flattened_data(), strict=True):
        if flag:
            fg[_bin(p)] += 1  # type: ignore[arg-type]
    grow = 2 * max(1, round(BG_GAP * n)) + 1
    near = list(_warp(cand_mask, al, n, 0).filter(ImageFilter.MaxFilter(grow)).get_flattened_data())
    x_lo, x_hi, y_lo, y_hi = box
    pixels: list[_RGB] = list(orig.get_flattened_data())  # type: ignore[arg-type]
    for i, p in enumerate(pixels):
        x, y = i % n, i // n
        if not near[i] and x_lo <= x < x_hi and y_lo <= y < y_hi:
            bg[_bin(p)] += 1
    nf, nb = max(1, sum(fg)), max(1, sum(bg))
    lut = []
    for f, b in zip(fg, bg, strict=True):
        # a colour the model never shows as product (an edge blend, a shadow) is not product
        pf, pb = f / nf, (b + 0.5) / nb
        lut.append(round(255 * pf / (pf + pb)))
    out = Image.new("L", (n, n))
    out.putdata(
        [
            lut[_bin(p)] if x_lo <= i % n < x_hi and y_lo <= i // n < y_hi else 0
            for i, p in enumerate(pixels)
        ]
    )
    return out


@dataclass(frozen=True, slots=True)
class _Level:
    orig: Image.Image
    cand: Image.Image
    core: Image.Image
    grown: Image.Image
    like: Image.Image

    @classmethod
    def at(cls, orig: Image.Image, cand: Image.Image, like: Image.Image, n: int) -> _Level:
        o = orig.resize((n, n), Image.Resampling.BOX)
        c = cand.resize((n, n), Image.Resampling.BOX)
        mask = _not_white_mask(c)
        edge = 3 if n < WORK_PX else 5
        return cls(
            o,
            c,
            mask.filter(ImageFilter.MinFilter(edge)).convert("RGB"),
            mask.filter(ImageFilter.MaxFilter(edge)),
            like.resize((n, n), Image.Resampling.BILINEAR),
        )

    def cost(self, al: Alignment) -> float:
        """Mean, over the frame, of the capped colour difference under the registered mask plus
        `DIFF_CAP` x P(product) where the mask leaves the photo out. The product's edge (one pixel
        either side, two from `WORK_PX` up) counts neither way, so a redrawn or blurred edge pulls
        the fit neither in nor out."""
        n = self.orig.width
        cw = _warp(self.cand, al, n, WHITE)
        diff = ImageChops.difference(self.orig, cw).point(_CAP_LUT)
        inside = sum(ImageStat.Stat(ImageChops.multiply(diff, _warp(self.core, al, n, 0))).sum)
        outside = ImageChops.invert(_warp(self.grown, al, n, 0))
        left_out = ImageStat.Stat(ImageChops.multiply(self.like, outside)).sum[0]
        drift = abs(al.sx - 1) + abs(al.sy - 1) + abs(al.dx) + abs(al.dy)
        return (inside / 3 + left_out * DIFF_CAP / 255) / (n * n) + TIE * drift


def _allowed(al: Alignment) -> bool:
    return (
        SCALE_MIN <= al.sx <= SCALE_MAX
        and SCALE_MIN <= al.sy <= SCALE_MAX
        and abs(al.sx / al.sy - 1) <= ASPECT_MAX
        and max(abs(al.dx), abs(al.dy)) <= SHIFT_MAX
    )


def _descend(level: _Level, al: Alignment) -> tuple[float, Alignment]:
    """A pattern search: shift and scale steps of `STEP_START` pixels, halved whenever none
    lowers the cost, down to half a pixel."""
    n = level.orig.width
    d = STEP_START / n
    cost = level.cost(al)
    for _ in range(4 * n):
        if d < 0.5 / n:
            break
        sx, sy, x, y = al.sx, al.sy, al.dx, al.dy
        moves = (
            Alignment(sx * (1 + d), sy * (1 + d), x, y),
            Alignment(sx * (1 - d), sy * (1 - d), x, y),
            Alignment(sx * (1 + d), sy, x, y),
            Alignment(sx * (1 - d), sy, x, y),
            Alignment(sx, sy * (1 + d), x, y),
            Alignment(sx, sy * (1 - d), x, y),
            Alignment(sx, sy, x + d, y),
            Alignment(sx, sy, x - d, y),
            Alignment(sx, sy, x, y + d),
            Alignment(sx, sy, x, y - d),
        )
        best: tuple[float, Alignment] | None = None
        for move in moves:
            if not _allowed(move):
                continue
            c = level.cost(move)
            if c < cost - 1e-9 and (best is None or c < best[0]):
                best = (c, move)
        if best is None:
            d /= 2
            continue
        cost, al = best
    return cost, al


def align(
    original: Image.Image, candidate: Image.Image, photo_size: tuple[int, int] | None = None
) -> Alignment:
    """Register the model's output onto the original's square: an exhaustive search over one
    scale and the shifts at `COARSE_PX`, then a descent over both scales and the shifts from the
    best few starts and the identity, refined once more after the likelihood is re-learnt."""
    side = original.width
    orig = _work(original, FIT_PX)
    cand = _work(candidate, FIT_PX)
    box = _photo_box(side, photo_size, FIT_PX)
    like = _likelihood(orig, cand, IDENTITY, box)
    coarse = _Level.at(orig, cand, like, COARSE_PX)
    reach = round(SHIFT_MAX * COARSE_PX)
    steps = math.floor(math.log(SCALE_MAX) / math.log(SCALE_STEP))
    found: list[tuple[float, Alignment]] = []
    for k in range(-steps, steps + 1):
        s = SCALE_STEP**k
        for ty in range(-reach, reach + 1):
            for tx in range(-reach, reach + 1):
                al = Alignment(s, s, tx / COARSE_PX, ty / COARSE_PX)
                if _allowed(al):
                    found.append((coarse.cost(al), al))
    found.sort(key=lambda f: f[0])
    best: tuple[float, Alignment] | None = None
    for start in [IDENTITY] + [al for _, al in found[:3]]:
        al = start
        cost = 0.0
        for n in (64, FIT_PX):
            cost, al = _descend(_Level.at(orig, cand, like, n), al)
        if best is None or cost < best[0]:
            best = (cost, al)
    assert best is not None  # noqa: S101 - the identity is always a start
    al = best[1]
    like = _likelihood(orig, cand, al, box)
    _, al = _descend(_Level.at(orig, cand, like, FIT_PX), al)
    for n in (WORK_PX, FINE_PX):
        _, al = _descend(_Level.at(_work(original, n), _work(candidate, n), like, n), al)
    return al


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


def _attached(suspect: bytearray, near: bytes, size: int) -> int:
    """How many suspect pixels join the product's ring through suspect pixels (4-connected): a
    cut-off strap or sleeve hangs off the product, background clutter of a product colour lies
    apart from it."""
    stack = [
        i
        for i in range(size * size)
        if suspect[i]
        and (
            (i % size and near[i - 1])
            or (i % size < size - 1 and near[i + 1])
            or (i >= size and near[i - size])
            or (i < size * (size - 1) and near[i + size])
        )
    ]
    seen = bytearray(size * size)
    for i in stack:
        seen[i] = 1
    count = 0
    while stack:
        i = stack.pop()
        count += 1
        x = i % size
        for j in (i - 1 if x else -1, i + 1 if x < size - 1 else -1, i - size, i + size):
            if 0 <= j < size * size and suspect[j] and not seen[j]:
                seen[j] = 1
                stack.append(j)
    return count


def judge(
    original: Image.Image,
    candidate: Image.Image,
    border: BorderProfile,
    *,
    photo_size: tuple[int, int] | None = None,
) -> Fidelity:
    """Register the edit onto the photo and score the registered mask. `original` is the square
    (`pad_square`); `photo_size` is the seller's photo inside it, outside which the original is
    padding, never product. An edit whose aspect differs from 1:1 by more than `ASPECT_TOL`, or
    with no product at all, fails at once. The returned `alignment` feeds `composite`."""
    ow, oh = original.size
    cw, ch = candidate.size
    if ow <= 0 or oh <= 0 or cw <= 0 or ch <= 0 or ow != oh:
        return _FAILED
    if abs(1 - cw / ch) > ASPECT_TOL:
        return _FAILED
    cand_rgb = _rgb(candidate)
    size = WORK_PX
    cand_small = _work(cand_rgb, size)
    if ImageStat.Stat(_not_white_mask(cand_small)).sum[0] == 0:
        return _FAILED
    al = align(original, cand_rgb, photo_size)
    orig_img = _work(original, size)
    cand_img = _warp(cand_small, al, size, WHITE)
    in_mask = _not_white_mask(cand_img)
    core = list(in_mask.filter(ImageFilter.MinFilter(CORE_ERODE)).get_flattened_data())
    near = in_mask.filter(ImageFilter.MaxFilter(HOLE_RING)).tobytes()
    masked = list(in_mask.get_flattened_data())
    orig: list[_RGB] = list(orig_img.get_flattened_data())  # type: ignore[arg-type]
    cand: list[_RGB] = list(cand_img.get_flattened_data())  # type: ignore[arg-type]
    bg = border.colour
    x_lo, x_hi, y_lo, y_hi = _photo_box(ow, photo_size, size)
    bins = (HIST_BINS + 1) ** 3
    hist_o, hist_c, hist_bg = [0] * bins, [0] * bins, [0] * bins
    # the same two histograms on a grid shifted half a bin: a flat colour on a bin edge (JPEG noise
    # of one level either side) splits on one grid and not on the other
    half = 256 // HIST_BINS // 2
    shift_o, shift_c = [0] * bins, [0] * bins
    inside = cored = agree = orig_n = both = bg_n = 0
    min_x = min_y = 1 << 30
    max_x = max_y = -1
    for i, (po, pc) in enumerate(zip(orig, cand, strict=True)):
        x, y = i % size, i // size
        in_photo = x_lo <= x < x_hi and y_lo <= y < y_hi
        in_orig = (
            in_photo and max(abs(po[0] - bg[0]), abs(po[1] - bg[1]), abs(po[2] - bg[2])) > ORIG_DIST
        )
        orig_n += in_orig
        if masked[i]:
            inside += 1
            both += in_orig
            min_x, max_x = min(min_x, x), max(max_x, x)
            min_y, max_y = min(min_y, y), max(max_y, y)
        elif in_photo and not near[i]:
            hist_bg[_bin(po)] += 1
            bg_n += 1
        if core[i]:
            cored += 1
            if max(abs(po[0] - pc[0]), abs(po[1] - pc[1]), abs(po[2] - pc[2])) <= COLOUR_TOL:
                agree += 1
            hist_o[_bin(po)] += 1
            hist_c[_bin(pc)] += 1
            shift_o[_bin(po, half)] += 1
            shift_c[_bin(pc, half)] += 1
    if inside == 0 or cored == 0:
        return _FAILED
    agreement = agree / cored
    colour_distance = min(_bhattacharyya(hist_o, hist_c), _bhattacharyya(shift_o, shift_c))
    mask_iou: float | None = None
    if border.uniform:
        union = orig_n + inside - both
        mask_iou = both / union if union else 0.0
    # CP-D20: photo pixels near the product, clear of the mask's edge, whose colour is commoner
    # under the product than in the photo's background
    product_like = {
        b
        for b, n in enumerate(hist_o)
        if n >= PALETTE_MIN * cored and n / cored > (hist_bg[b] / bg_n if bg_n else 0.0)
    }
    x0, x1, y0, y1 = x_lo, x_hi - 1, y_lo, y_hi - 1
    suspect = bytearray(size * size)
    for y in range(y0, y1 + 1):
        row = y * size
        for x in range(x0, x1 + 1):
            i = row + x
            po = orig[i]
            if not near[i] and not _near_white(po) and _bin(po) in product_like:
                suspect[i] = 1
    suspects = _attached(suspect, near, size)
    hole_share = min(1.0, suspects / inside)
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
        alignment=al,
    )


def composite(
    original_full: Image.Image, candidate: Image.Image, alignment: Alignment = IDENTITY
) -> Image.Image:
    """The seller's own pixels under the registered product mask, pure white everywhere else.
    `original_full` is the square padded with `PAD_COLOUR` at the seller's resolution; the model's
    output only says which of its pixels are product (design §2.7)."""
    original = _rgb(original_full)
    mask = _warp(_not_white_mask(_rgb(candidate)), alignment, original.width, 0)
    mask = mask.filter(ImageFilter.MinFilter(ERODE)).filter(ImageFilter.GaussianBlur(FEATHER))
    return Image.composite(original, Image.new("RGB", original.size, WHITE), mask)
