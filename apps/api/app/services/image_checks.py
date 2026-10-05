"""Deterministic Pillow image checks.

`check_image` measures one uploaded image against the resolved image rules for a marketplace and
slot and returns an `ImageReport`: file size, format (magic bytes), corruption (JPEG scan walk,
then Pillow open, verify, reopen and load), EXIF orientation, resolution, aspect ratio and
background. Every threshold comes from the rules; the module holds no marketplace numbers.

Pure library: no database, no model client, no logger, no global state, and no Pillow global is
read or written, so calls are deterministic and safe to run in threads. Hostile input never
raises: Pillow calls on the untrusted image sit inside two narrow boundaries (decode, EXIF) that
map any exception to a report entry. Our own code stays outside them, so a misconfigured rule
(`ValueError`) or a library bug still raises.
"""

from __future__ import annotations

import struct
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from io import BytesIO
from types import MappingProxyType
from typing import Final
from uuid import UUID

from mkos_schemas.codes import ImageIssueCode
from mkos_schemas.enums import ImageSlot, Marketplace, Severity
from mkos_schemas.images import CheckName, ImageCheck, ImageIssue, ImageReport
from mkos_schemas.rules import ImageRequirement, Rule
from PIL import Image, ImageChops

__all__ = [
    "CHECK_CODES",
    "DECODE_FORMATS",
    "DEFAULT_SUGGESTIONS",
    "MAX_JPEG_SCANS",
    "MAX_PIXELS",
    "MAX_SIDE",
    "check_image",
]

DECODE_FORMATS: Final = ("JPEG", "PNG", "TIFF")
# Per-call decode budget: Pillow stores 8-bit multi-band modes at 4 B/px, so 40 M px is ~160 MB
# of pixel storage. Checked on the header, before verify() or load() allocates anything.
MAX_PIXELS: Final = 40_000_000
# A thin strip (1 x 40 M) passes MAX_PIXELS yet costs one row pointer per line and full-image band
# crops; capping each side keeps that path to a few x 20 k px. Real product photos are far smaller.
MAX_SIDE: Final = 20_000
# A progressive JPEG decodes every scan; real encoders emit about ten.
MAX_JPEG_SCANS: Final = 64
ASPECT_TOLERANCE_PERCENT: Final = 1
# Background band: depth BAND_PERCENT % of each side (at least BAND_MIN_PX), a pixel matches when
# opaque and every channel is within CHANNEL_TOLERANCE of the target, and a region passes at
# MIN_MATCH % matching. On an edge, one off run is forgiven as the product's contact only where the
# line just inside the band is off too (the product continues inward) and the run is shorter than
# MAX_RUN_PERCENT % of the edge. At most MAX_WIDE_RUN_EDGES edges may have a forgiven run over
# WIDE_RUN_PERCENT % of the edge: a grey card with rounded or cut white corners continues inward on
# every edge, while an inscribed disc covers about 28 % of each. A full-bleed product touching three
# or four edges over half their length fails closed (vision confirms). When two opposite edges both
# have a wide run and the runs line up (start and end within max(BAND_MIN_PX, 1 % of the edge)),
# the full line just inside each run boundary is read: off over MAX_RUN_PERCENT % of it is a
# straight boundary, a photo letterboxed or pillarboxed onto the canvas, and fails. Calibrated on
# synthetic images; revisited when real images arrive.
BAND_MIN_PX: Final = 2
BAND_PERCENT: Final = 2
CHANNEL_TOLERANCE: Final = 5
MIN_MATCH: Final = 99
MAX_RUN_PERCENT: Final = 90
WIDE_RUN_PERCENT: Final = 50
MAX_WIDE_RUN_EDGES: Final = 2

CHECK_CODES: Final[Mapping[CheckName, ImageIssueCode]] = MappingProxyType(
    {
        "file_size": ImageIssueCode.FILE_TOO_LARGE,
        "format": ImageIssueCode.FORMAT_UNSUPPORTED,
        "corruption": ImageIssueCode.CORRUPT,
        "resolution": ImageIssueCode.RESOLUTION_LOW,
        "aspect_ratio": ImageIssueCode.WRONG_ASPECT,
        "background": ImageIssueCode.BACKGROUND_OFF,
    }
)
DEFAULT_SUGGESTIONS: Final[Mapping[ImageIssueCode, str]] = MappingProxyType(
    {
        ImageIssueCode.BACKGROUND_OFF: "Background removal / replacement",
        ImageIssueCode.RESOLUTION_LOW: "Upscale or re-export",
        ImageIssueCode.WRONG_ASPECT: "Crop to correct ratio",
        ImageIssueCode.FORMAT_UNSUPPORTED: "Re-export the image as JPEG, PNG or TIFF",
        ImageIssueCode.FILE_TOO_LARGE: "Reduce the file size or pixel dimensions and re-export",
        ImageIssueCode.CORRUPT: "The file could not be decoded; re-export it and upload again",
    }
)
_DOWNSCALE: Final = "Downscale and re-export"

_VISION_ONLY: Final = frozenset({"fill_ratio", "text_overlay", "watermark", "logo"})
_RESOLUTION_RULES: Final = frozenset({"min_resolution", "recommended_resolution"})
_FORMAT_ALIASES: Final[Mapping[str, str]] = MappingProxyType({"jpg": "jpeg", "tif": "tiff"})
_ORIENTATION_TAG: Final = 0x0112
_SWAPPING_ORIENTATIONS: Final = frozenset({5, 6, 7, 8})
_SEVERITY_RANK: Final[Mapping[Severity, int]] = MappingProxyType(
    {Severity.ERROR: 0, Severity.WARNING: 1}
)

_Selected = tuple[Rule, ImageRequirement]


# --- report assembly ---


@dataclass(frozen=True, slots=True)
class _Eval:
    passed: bool
    severity: Severity
    suggestion: str


class _Checks:
    """Evaluations per check, in first-evaluation order."""

    def __init__(self) -> None:
        self._evals: dict[CheckName, list[_Eval]] = {}

    def add(
        self,
        check: CheckName,
        passed: bool,
        rule: Rule | None = None,
        *,
        default: str | None = None,
    ) -> None:
        """Record one evaluation. No rule = an intrinsic check, error severity."""
        severity = Severity.ERROR if rule is None else rule.severity
        suggestion = rule.rule_def.suggestion if rule is not None else None
        if suggestion is None:
            code = CHECK_CODES.get(check)
            suggestion = default or (DEFAULT_SUGGESTIONS[code] if code is not None else check)
        self._evals.setdefault(check, []).append(_Eval(passed, severity, suggestion))

    def error_failed(self, check: CheckName) -> bool:
        return any(
            not e.passed and e.severity is Severity.ERROR for e in self._evals.get(check, ())
        )

    def report(
        self, *, image_id: UUID, marketplace: Marketplace, slot: ImageSlot, now: datetime
    ) -> ImageReport:
        checks: list[ImageCheck] = []
        issues: list[ImageIssue] = []
        passed_all = True
        for check, evals in self._evals.items():
            failing = [e for e in evals if not e.passed]
            checks.append(ImageCheck(check=check, method="deterministic", passed=not failing))
            if not failing:
                continue
            worst = min(failing, key=lambda e: _SEVERITY_RANK[e.severity])
            issues.append(
                ImageIssue(code=CHECK_CODES[check], check=check, suggestion=worst.suggestion)
            )
            if worst.severity is Severity.ERROR:
                passed_all = False
        return ImageReport(
            image_id=image_id,
            marketplace=marketplace,
            slot=slot,
            pass_=passed_all,
            checks=checks,
            issues=issues,
            processed_at=now,
        )


# --- rule selection ---


def _require(rule: Rule, req: ImageRequirement) -> None:
    """A selected rule without the param its check needs is a seed bug, never an image property."""
    p = req.params
    missing: str | None = None
    if req.check in _RESOLUTION_RULES and p.min_px is None and p.max_px is None:
        missing = "min_px or max_px"
    elif req.check == "aspect_ratio" and (p.ratio_w is None or p.ratio_h is None):
        missing = "ratio_w and ratio_h"
    elif req.check == "background" and p.rgb is None:
        missing = "rgb"
    elif req.check == "max_bytes" and p.max_bytes is None:
        missing = "max_bytes"
    elif req.check == "format" and not p.allowed_formats:
        missing = "allowed_formats"
    if missing is not None:
        raise ValueError(f"image rule {rule.id}: {req.check} needs {missing}")


def _select(rules: Sequence[Rule], marketplace: Marketplace, slot: ImageSlot) -> list[_Selected]:
    """Enabled image rules for this marketplace and slot (or `any`), vision-only checks dropped.
    Rules arrive resolved: category shadowing and versions are the caller's."""
    selected: list[_Selected] = []
    for rule in rules:
        req = rule.rule_def
        if not (rule.enabled and isinstance(req, ImageRequirement)):
            continue
        if rule.marketplace != marketplace or req.slot not in (slot.value, "any"):
            continue
        if req.check in _VISION_ONLY:
            continue
        _require(rule, req)
        selected.append((rule, req))
    return selected


# --- format and JPEG structure ---


def _sniff(data: bytes) -> str | None:
    """The decoder name from magic bytes only; anything unlisted is unsupported."""
    if data.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if data.startswith((b"II*\x00", b"MM\x00*")):
        return "TIFF"
    return None


def _jpeg_scan_count(data: bytes) -> int | None:
    """Count SOS segments by walking JPEG marker segments (never a raw `FF DA` count: APPn
    payloads such as an EXIF thumbnail hold those bytes). Stops early once over the cap.
    `None`: no SOI, or a segment length runs past the end. Truncation inside entropy-coded data
    ends the walk; the decoder judges it."""
    size = len(data)
    if not data.startswith(b"\xff\xd8"):
        return None
    pos, scans = 2, 0
    while True:
        pos = data.find(
            b"\xff", pos
        )  # extraneous bytes before a marker are skipped, as libjpeg does
        if pos == -1:
            return scans
        while pos < size and data[pos] == 0xFF:  # fill bytes
            pos += 1
        if pos >= size:
            return scans
        marker = data[pos]
        pos += 1
        if marker == 0xD9:  # EOI
            return scans
        if marker in (0x00, 0x01) or 0xD0 <= marker <= 0xD8:  # no length field
            continue
        if pos + 2 > size:
            return None
        (length,) = struct.unpack_from(">H", data, pos)
        if length < 2 or pos + length > size:
            return None
        pos += length
        if marker != 0xDA:
            continue
        scans += 1
        if scans > MAX_JPEG_SCANS:
            return scans
        while True:  # entropy-coded data: FF 00 is stuffing, FF D0-D7 is RSTn
            pos = data.find(b"\xff", pos)
            if pos == -1 or pos + 1 >= size:
                return scans
            following = data[pos + 1]
            if following == 0x00 or 0xD0 <= following <= 0xD7:
                pos += 2
                continue
            break


# --- decode (the only Pillow calls on the untrusted image) ---


class _Failure(Enum):
    TOO_LARGE = "too_large"
    CORRUPT = "corrupt"


@dataclass(frozen=True, slots=True)
class _Edge:
    crop: Image.Image  # band depth + 1 line
    horizontal: bool  # runs along the top or bottom edge
    inner_first: bool  # the inward line is the first row or column (bottom, right edges)


@dataclass(frozen=True, slots=True)
class _BandRegions:
    """RGBA crops of the background band. Each edge crop is one line deeper than the band: that
    line, just inside the band, shows whether an off run continues inward. `whole` is set instead
    when the image is too small to hold separate corners and edges."""

    corners: tuple[Image.Image, ...] = ()
    edges: tuple[_Edge, ...] = ()  # top, bottom, left, right
    whole: Image.Image | None = None
    size: tuple[int, int] = (0, 0)  # full image width, height
    inset: tuple[int, int] = (0, 0)  # band depth bx, by: where the edge strips start along the edge


@dataclass(frozen=True, slots=True)
class _Decoded:
    width: int  # measured: EXIF orientation 5-8 swapped
    height: int
    background: tuple[bool, ...]  # one verdict per requested background colour, in order


_Line = Callable[[tuple[int, int, int, int]], Image.Image]


def _orientation(img: Image.Image) -> int:
    """EXIF orientation 1-8; anything missing, malformed or unreadable counts as 1."""
    try:
        value = img.getexif().get(_ORIENTATION_TAG)
    except Exception:  # EXIF boundary: hostile IFDs raise assorted types; orientation 1 by rule
        return 1
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 8:
        return value
    return 1


def _rgba(crop: Image.Image) -> Image.Image:
    """8-bit RGBA; 16-bit and 32-bit integer or float crops are scaled from 16-bit range first."""
    if crop.mode in ("I", "F") or crop.mode.startswith("I;16"):
        crop = crop.convert("F").point(lambda v: v / 257).convert("L")
    return crop.convert("RGBA")


def _band_regions(img: Image.Image) -> _BandRegions:
    """Crop the band from the loaded image, then convert only the crops (never the full image,
    which would double the decode memory)."""
    w, h = img.size
    by = max(BAND_MIN_PX, h * BAND_PERCENT // 100)
    bx = max(BAND_MIN_PX, w * BAND_PERCENT // 100)
    if w <= 2 * bx or h <= 2 * by:
        return _BandRegions(whole=_rgba(img.crop((0, 0, w, h))))
    corners = (
        (0, 0, bx, by),
        (w - bx, 0, w, by),
        (0, h - by, bx, h),
        (w - bx, h - by, w, h),
    )
    edges = (  # (box including the inward line, horizontal, inward line first)
        ((bx, 0, w - bx, by + 1), True, False),
        ((bx, h - by - 1, w - bx, h), True, True),
        ((0, by, bx + 1, h - by), False, False),
        ((w - bx - 1, by, w, h - by), False, True),
    )
    return _BandRegions(
        corners=tuple(_rgba(img.crop(box)) for box in corners),
        edges=tuple(_Edge(_rgba(img.crop(box)), hz, first) for box, hz, first in edges),
        size=(w, h),
        inset=(bx, by),
    )


_DECODE_TOO_LARGE: Final = (
    Image.DecompressionBombError,
    Image.DecompressionBombWarning,
    MemoryError,
)


def _decode(
    data: bytes, fmt: str, backgrounds: Sequence[tuple[int, int, int]]
) -> _Decoded | _Failure:
    """Open with the sniffed decoder only, cap pixels and side length on the header, verify,
    reopen and load frame 0, then crop the background band and judge each requested background
    colour while the loaded image is still open (a padded-photo test reads one full line). Pixels
    are never rotated. The judging runs outside the decode boundary, so a bug in it raises."""
    try:
        with Image.open(BytesIO(data), formats=[fmt]) as probe:
            width, height = probe.size
            if width * height > MAX_PIXELS or max(width, height) > MAX_SIDE:
                return _Failure.TOO_LARGE
            if width == 0 or height == 0:
                return _Failure.CORRUPT
            probe.verify()
        img = Image.open(BytesIO(data), formats=[fmt])
    except _DECODE_TOO_LARGE:
        return _Failure.TOO_LARGE
    except Exception:  # decode boundary: any decoder failure on hostile bytes is CORRUPT
        return _Failure.CORRUPT
    with img:
        try:
            img.load()
            orientation = _orientation(img)
            band = _band_regions(img) if backgrounds else None
        except _DECODE_TOO_LARGE:
            return _Failure.TOO_LARGE
        except Exception:  # decode boundary (as above)
            return _Failure.CORRUPT

        def line(box: tuple[int, int, int, int]) -> Image.Image:
            return _rgba(img.crop(box))

        verdicts = tuple(_background_passes(band, rgb, line) for rgb in backgrounds) if band else ()
    if orientation in _SWAPPING_ORIENTATIONS:
        width, height = height, width
    return _Decoded(width, height, verdicts)


# --- geometry ---


def _resolution(checks: _Checks, selected: list[_Selected], width: int, height: int) -> None:
    for rule, req in selected:
        p = req.params
        if p.min_px is not None and min(width, height) < p.min_px:
            checks.add("resolution", False, rule)
        elif p.max_px is not None and max(width, height) > p.max_px:
            checks.add("resolution", False, rule, default=_DOWNSCALE)
        else:
            checks.add("resolution", True, rule)


def _ratio_matches(req: ImageRequirement, width: int, height: int) -> bool:
    rw, rh = req.params.ratio_w, req.params.ratio_h
    if rw is None or rh is None:  # unreachable after _require; keeps the types honest
        raise ValueError("aspect_ratio needs ratio_w and ratio_h")
    return abs(width * rh - height * rw) * 100 <= height * rw * ASPECT_TOLERANCE_PERCENT


def _aspect(checks: _Checks, selected: list[_Selected], width: int, height: int) -> None:
    """Rules of one slot that differ in pack are alternatives: the slot group passes when every
    rule of some pack matches. Every slot group (`any` and the image's slot) must pass."""
    groups: dict[str, dict[str, list[_Selected]]] = {}
    for rule, req in selected:
        groups.setdefault(req.slot, {}).setdefault(req.pack, []).append((rule, req))
    for packs in groups.values():
        if any(all(_ratio_matches(req, width, height) for _, req in m) for m in packs.values()):
            checks.add("aspect_ratio", True)
            continue
        members = [rule for m in packs.values() for rule, _ in m]
        worst = min(members, key=lambda r: _SEVERITY_RANK[r.severity])
        checks.add("aspect_ratio", False, worst)


# --- background ---

_ALPHA_OPAQUE: Final = [255 if v == 255 else 0 for v in range(256)]
_INVERT: Final = [255 - v for v in range(256)]


def _match_mask(rgba: Image.Image, rgb: tuple[int, int, int]) -> Image.Image:
    """`L` mask: 255 where the pixel is opaque and within tolerance of `rgb`, else 0."""
    lut: list[int] = []
    for target in rgb:
        lut += [255 if abs(v - target) <= CHANNEL_TOLERANCE else 0 for v in range(256)]
    r, g, b, a = rgba.point(lut + _ALPHA_OPAQUE).split()
    return ImageChops.multiply(ImageChops.multiply(r, g), ImageChops.multiply(b, a))


def _mostly_matching(matching: int, total: int) -> bool:
    return matching * 100 >= total * MIN_MATCH


def _region_passes(mask: Image.Image) -> bool:
    return _mostly_matching(mask.histogram()[255], mask.width * mask.height)


def _edge_passes(mask: Image.Image, edge: _Edge) -> tuple[bool, tuple[int, int] | None]:
    """Split off the inward line, collapse the band strip to a 1 px profile along the edge
    (mismatches per position), forgive the longest run of positions where the inward line is off
    (a product touching that edge and continuing inward) if it is under MAX_RUN_PERCENT % of the
    edge, and judge the rest of the strip. Returns (passes, the forgiven run as (start, length)
    along the strip when it covers over WIDE_RUN_PERCENT % of the edge, else None).
    """
    w, h = mask.size
    if edge.horizontal:
        length, depth, size = w, h - 1, (w, 1)
        cut = 1 if edge.inner_first else h - 1
        inner_box = (0, cut - 1, w, cut) if edge.inner_first else (0, cut, w, h)
        band_box = (0, cut, w, h) if edge.inner_first else (0, 0, w, cut)
    else:
        length, depth, size = h, w - 1, (1, h)
        cut = 1 if edge.inner_first else w - 1
        inner_box = (cut - 1, 0, cut, h) if edge.inner_first else (cut, 0, w, h)
        band_box = (cut, 0, w, h) if edge.inner_first else (0, 0, cut, h)
    inward_off = [v == 0 for v in mask.crop(inner_box).tobytes()]
    band = mask.crop(band_box)
    profile = band.point(_INVERT).convert("F").resize(size, Image.Resampling.BOX)
    means = struct.unpack(f"={length}f", profile.tobytes())
    counts = [round(m * depth / 255) for m in means]
    best_start = best_len = 0
    start: int | None = None
    for i, off in enumerate([*inward_off, False]):
        if off and start is None:
            start = i
        elif not off and start is not None:
            if i - start > best_len:
                best_start, best_len = start, i - start
            start = None
    if best_len * 100 >= length * MAX_RUN_PERCENT:
        best_len = 0
    wide = (best_start, best_len) if best_len * 100 > length * WIDE_RUN_PERCENT else None
    total = (length - best_len) * depth
    if total == 0:
        return True, wide
    mismatching = sum(counts) - sum(counts[best_start : best_start + best_len])
    return _mostly_matching(total - mismatching, total), wide


def _straight_boundary(
    band: _BandRegions,
    rgb: tuple[int, int, int],
    line: _Line,
    horizontal: bool,
    runs: tuple[tuple[int, int], tuple[int, int]],
) -> bool:
    """True when two opposite edges' wide runs line up and the full line just inside either run
    boundary is off over MAX_RUN_PERCENT %: a photo padded onto the canvas, not a product."""
    (s0, n0), (s1, n1) = runs
    w, h = band.size
    length = (w if horizontal else h) - 2 * (band.inset[0] if horizontal else band.inset[1])
    tolerance = max(BAND_MIN_PX, length // 100)
    if abs(s0 - s1) > tolerance or abs((s0 + n0) - (s1 + n1)) > tolerance:
        return False
    offset = band.inset[0] if horizontal else band.inset[1]
    for at in (offset + max(s0, s1), offset + min(s0 + n0, s1 + n1) - 1):
        crop = line((at, 0, at + 1, h) if horizontal else (0, at, w, at + 1))
        off = crop.width * crop.height - _match_mask(crop, rgb).histogram()[255]
        if off * 100 >= crop.width * crop.height * MAX_RUN_PERCENT:
            return True
    return False


def _background_passes(band: _BandRegions, rgb: tuple[int, int, int], line: _Line) -> bool:
    if band.whole is not None:
        return _region_passes(_match_mask(band.whole, rgb))
    if not all(_region_passes(_match_mask(c, rgb)) for c in band.corners):
        return False
    runs: list[tuple[int, int] | None] = []
    for edge in band.edges:
        passes, wide = _edge_passes(_match_mask(edge.crop, rgb), edge)
        if not passes:
            return False
        runs.append(wide)
    if sum(r is not None for r in runs) > MAX_WIDE_RUN_EDGES:
        return False
    for horizontal, (a, b) in ((True, (runs[0], runs[1])), (False, (runs[2], runs[3]))):
        if (
            a is not None
            and b is not None
            and _straight_boundary(band, rgb, line, horizontal, (a, b))
        ):
            return False
    return True


# --- entry point ---


def check_image(
    data: bytes,
    *,
    image_id: UUID,
    marketplace: Marketplace,
    slot: ImageSlot,
    rules: Sequence[Rule],
    now: datetime,
) -> ImageReport:
    """Run every deterministic check that applies and return the report. Only an error-severity
    failure of `file_size`, `format` or `corruption` stops early (nothing after is measurable)."""
    selected = _select(rules, marketplace, slot)
    by_check: dict[str, list[_Selected]] = {}
    for item in selected:
        by_check.setdefault(item[1].check, []).append(item)
    checks = _Checks()

    def done() -> ImageReport:
        return checks.report(image_id=image_id, marketplace=marketplace, slot=slot, now=now)

    for rule, req in by_check.get("max_bytes", ()):
        limit = req.params.max_bytes
        checks.add("file_size", limit is not None and len(data) <= limit, rule)
    if checks.error_failed("file_size"):
        return done()

    fmt = _sniff(data)
    checks.add("format", fmt is not None)
    if fmt is None:
        return done()
    for rule, req in by_check.get("format", ()):
        allowed = {_FORMAT_ALIASES.get(n.lower(), n.lower()) for n in req.params.allowed_formats}
        checks.add("format", fmt.lower() in allowed, rule)
    if checks.error_failed("format"):
        return done()

    if fmt == "JPEG":
        scans = _jpeg_scan_count(data)
        if scans is None or scans > MAX_JPEG_SCANS:
            checks.add("corruption", False)
            return done()
    background = by_check.get("background", [])
    rgbs: list[tuple[int, int, int]] = []
    for rule, req in background:
        rgb = req.params.rgb
        if rgb is None:  # unreachable after _require; keeps the types honest
            raise ValueError(f"image rule {rule.id}: background needs rgb")
        rgbs.append(rgb)
    decoded = _decode(data, fmt, rgbs)
    if isinstance(decoded, _Failure):
        checks.add("file_size" if decoded is _Failure.TOO_LARGE else "corruption", False)
        return done()
    checks.add("corruption", True)
    checks.add("exif_orientation", True)

    resolution = [item for item in selected if item[1].check in _RESOLUTION_RULES]
    _resolution(checks, resolution, decoded.width, decoded.height)
    _aspect(checks, by_check.get("aspect_ratio", []), decoded.width, decoded.height)
    for (rule, _req), verdict in zip(background, decoded.background, strict=True):
        checks.add("background", verdict, rule)
    return done()
