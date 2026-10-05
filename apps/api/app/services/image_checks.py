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
from collections.abc import Mapping, Sequence
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
from PIL import Image

__all__ = [
    "CHECK_CODES",
    "DECODE_FORMATS",
    "DEFAULT_SUGGESTIONS",
    "MAX_JPEG_SCANS",
    "MAX_PIXELS",
    "check_image",
]

DECODE_FORMATS: Final = ("JPEG", "PNG", "TIFF")
# Per-call decode budget: Pillow stores 8-bit multi-band modes at 4 B/px, so 40 M px is ~160 MB
# of pixel storage. Checked on the header, before verify() or load() allocates anything.
MAX_PIXELS: Final = 40_000_000
# A progressive JPEG decodes every scan; real encoders emit about ten.
MAX_JPEG_SCANS: Final = 64
ASPECT_TOLERANCE_PERCENT: Final = 1

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
class _Decoded:
    width: int  # measured: EXIF orientation 5-8 swapped
    height: int


def _orientation(img: Image.Image) -> int:
    """EXIF orientation 1-8; anything missing, malformed or unreadable counts as 1."""
    try:
        value = img.getexif().get(_ORIENTATION_TAG)
    except Exception:  # EXIF boundary: hostile IFDs raise assorted types; orientation 1 by rule
        return 1
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 8:
        return value
    return 1


def _decode(data: bytes, fmt: str) -> _Decoded | _Failure:
    """Open with the sniffed decoder only, cap pixels on the header, verify, reopen and load
    frame 0. Pixels are never rotated."""
    try:
        with Image.open(BytesIO(data), formats=[fmt]) as probe:
            width, height = probe.size
            if width * height > MAX_PIXELS:
                return _Failure.TOO_LARGE
            if width == 0 or height == 0:
                return _Failure.CORRUPT
            probe.verify()
        with Image.open(BytesIO(data), formats=[fmt]) as img:
            img.load()
            orientation = _orientation(img)
    except (Image.DecompressionBombError, Image.DecompressionBombWarning, MemoryError):
        return _Failure.TOO_LARGE
    except Exception:  # decode boundary: any decoder failure on hostile bytes is CORRUPT
        return _Failure.CORRUPT
    if orientation in _SWAPPING_ORIENTATIONS:
        width, height = height, width
    return _Decoded(width, height)


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
    decoded = _decode(data, fmt)
    if isinstance(decoded, _Failure):
        checks.add("file_size" if decoded is _Failure.TOO_LARGE else "corruption", False)
        return done()
    checks.add("corruption", True)
    checks.add("exif_orientation", True)

    resolution = [item for item in selected if item[1].check in _RESOLUTION_RULES]
    _resolution(checks, resolution, decoded.width, decoded.height)
    _aspect(checks, by_check.get("aspect_ratio", []), decoded.width, decoded.height)
    return done()
