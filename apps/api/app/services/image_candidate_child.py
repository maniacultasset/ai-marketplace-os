"""The image candidate child process (SI7 T8): `python -m app.services.image_candidate_child`.

Untrusted pixels (the seller's photo, the model's output) are decoded only here, in a child with
the image children's environment allow-list and limits. It reads one JSON request on stdin,
`{"op", "inputs": {name: base64}, "params": {...}}`, and writes one JSON reply:
`{"ok": true, "image_b64", "width", "height", "fidelity"?, "border"?}` or
`{"ok": false, "reason", "fidelity"?}`. Every reply image is a fresh JPEG q92 with no metadata.

Ops:
- `prepare`: the vision copy padded to a square with its border colour, JPEG q90, at most 1024 px
  (the model's input, CP-D5).
- `finish_white`: decode the model output (PNG, JPEG or WebP only, pixel and side caps), judge it
  against the padded clean copy (`fidelity`), keep the seller's pixels (`composite`), whiten a
  near-white uniform background left by the model, frame it and run the main-image checks.
- `fix`: frame the clean copy (`image_fix.fix`) and run the main-image checks.
- `size_chart`: render the size chart.

A verdict (`too_small`, `source_unusable`, `fidelity`, `not_white`) is an `ok: false` reply. A
framed output is never `ok` unless it passes every deterministic main check of every marketplace
passed in: a failed `background` is `not_white`, any other failure `source_unusable`. Anything
else (a crash, the CPU or memory limit) is a non-zero exit, which the runner treats as an
infrastructure failure, never a verdict (CP-D19).

Imports Pillow, mkos_schemas, the stdlib and the pure SI7 libraries only (import contract
`image-check-child-is-pure`).
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import sys
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from mkos_schemas.enums import ImageSlot, Marketplace
from mkos_schemas.rules import Rule
from PIL import Image, ImageOps

from app.services.image_check_child import set_limits
from app.services.image_checks import MAX_PIXELS, MAX_SIDE, check_image
from app.services.image_fidelity import composite, judge, pad_square
from app.services.image_fix import (
    BorderProfile,
    FixRefused,
    border_profile,
    fix,
    frame_square,
    whiten_border,
)
from app.services.size_chart_image import ChartRefused, render_size_chart

OPS: Final = frozenset({"prepare", "finish_white", "fix", "size_chart"})
OUTPUT_FORMATS: Final = ("PNG", "JPEG", "WEBP")
CLEAN_FORMATS: Final = ("PNG", "JPEG")
MODEL_INPUT_PX: Final = 1024
MODEL_INPUT_QUALITY: Final = 90
REPLY_QUALITY: Final = 92
EXIT_BAD_REQUEST: Final = 2


class BadRequest(Exception):  # noqa: N818 - an exit code, not an error report
    """The request itself is malformed: exit 2 (the runner's infrastructure failure)."""


def _decode(data: bytes, formats: tuple[str, ...]) -> Image.Image | None:
    """An RGB image, or None for a type outside `formats`, a bomb or a broken file."""
    try:
        with Image.open(io.BytesIO(data), formats=list(formats)) as probe:
            w, h = probe.size
            if w < 1 or h < 1 or w * h > MAX_PIXELS or max(w, h) > MAX_SIDE:
                return None
            probe.load()
            upright = ImageOps.exif_transpose(probe)
            if upright.mode in ("RGBA", "LA", "P"):
                rgba = upright.convert("RGBA")
                base = Image.new("RGB", rgba.size, (255, 255, 255))
                base.paste(rgba, mask=rgba.getchannel("A"))
                return base
            return upright.convert("RGB")
    except Exception:  # hostile input never crashes the child: any decode failure is a verdict
        return None


def _jpeg(img: Image.Image, quality: int = REPLY_QUALITY) -> bytes:
    fresh = img.copy()
    fresh.info = {}
    out = io.BytesIO()
    fresh.save(out, format="JPEG", quality=quality, optimize=False)
    return out.getvalue()


def _ok(img: Image.Image, data: bytes, **extra: Any) -> dict[str, Any]:
    return {
        "ok": True,
        "image_b64": base64.b64encode(data).decode("ascii"),
        "width": img.width,
        "height": img.height,
        **extra,
    }


def _refused(reason: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "reason": reason, **extra}


def _border_json(prof: BorderProfile) -> dict[str, Any]:
    return {"uniform": prof.uniform, "near_white": prof.near_white, "colour": list(prof.colour)}


def _input(inputs: Mapping[str, Any], name: str) -> bytes:
    value = inputs.get(name)
    if not isinstance(value, str):
        raise BadRequest(name)
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise BadRequest(name) from None


def _rules(params: Mapping[str, Any]) -> dict[Marketplace, list[Rule]]:
    raw = params.get("rules")
    if not isinstance(raw, Mapping):
        raise BadRequest("rules")
    try:
        return {Marketplace(m): [Rule.model_validate(r) for r in rs] for m, rs in raw.items()}
    except (ValueError, TypeError):
        raise BadRequest("rules") from None


def _main_verdict(data: bytes, params: Mapping[str, Any]) -> str | None:
    """None when the framed output passes every deterministic main check, else the reason."""
    try:
        image_id = UUID(str(params["image_id"]))
        now = datetime.fromisoformat(str(params["now"]))
    except (KeyError, ValueError):
        raise BadRequest("image_id or now") from None
    failed: set[str] = set()
    for market, rules in _rules(params).items():
        report = check_image(
            data, image_id=image_id, marketplace=market, slot=ImageSlot.MAIN, rules=rules, now=now
        )
        failed |= {c.check for c in report.checks if c.method == "deterministic" and not c.passed}
    if not failed:
        return None
    return "not_white" if "background" in failed else "source_unusable"


def _framed(img: Image.Image, params: Mapping[str, Any], **extra: Any) -> dict[str, Any]:
    data = _jpeg(img)
    verdict = _main_verdict(data, params)
    if verdict is not None:
        return _refused(verdict, **extra)
    return _ok(img, data, **extra)


def _prepare(inputs: Mapping[str, Any]) -> dict[str, Any]:
    img = _decode(_input(inputs, "vision"), ("JPEG",))
    if img is None:
        return _refused("source_unusable")
    prof = border_profile(img)
    square = pad_square(img, prof.colour)
    if square.width > MODEL_INPUT_PX:
        square = square.resize((MODEL_INPUT_PX, MODEL_INPUT_PX), Image.Resampling.LANCZOS)
    return _ok(square, _jpeg(square, MODEL_INPUT_QUALITY), border=_border_json(prof))


def _finish_white(inputs: Mapping[str, Any], params: Mapping[str, Any]) -> dict[str, Any]:
    output = _decode(_input(inputs, "output"), OUTPUT_FORMATS)
    if output is None:
        return _refused("source_unusable")
    clean = _decode(_input(inputs, "clean"), CLEAN_FORMATS)
    if clean is None:
        return _refused("source_unusable")
    if not border_profile(output).near_white:  # the edit left a coloured background
        return _refused("not_white")
    prof = border_profile(clean)
    original = pad_square(clean, prof.colour)
    score = judge(original, output, prof)
    fidelity = score.as_json()
    if not score.passed:
        return _refused("fidelity", fidelity=fidelity)
    try:
        merged = composite(original, output)
        # The model's background may itself be a near-white grey: whiten it deterministically,
        # as `fix` does (the seller's product pixels are outside the background mask).
        merged_border = border_profile(merged)
        if merged_border.uniform and merged_border.near_white:
            merged = whiten_border(merged, merged_border)
        framed = frame_square(merged)
    except FixRefused as refused:
        return _refused(refused.reason, fidelity=fidelity)
    return _framed(framed, params, fidelity=fidelity)


def _fix(inputs: Mapping[str, Any], params: Mapping[str, Any]) -> dict[str, Any]:
    clean = _decode(_input(inputs, "clean"), CLEAN_FORMATS)
    if clean is None:
        return _refused("source_unusable")
    try:
        framed = fix(clean)
    except FixRefused as refused:
        return _refused(refused.reason)
    return _framed(framed, params)


def _size_chart(params: Mapping[str, Any]) -> dict[str, Any]:
    chart, fibres = params.get("chart"), params.get("fibres")
    if not isinstance(chart, Mapping) or (fibres is not None and not isinstance(fibres, list)):
        raise BadRequest("chart")
    try:
        data = render_size_chart(chart, fibres)
    except ChartRefused:
        return _refused("source_unusable")
    img = Image.open(io.BytesIO(data))
    return _ok(img, data)


def run(stdin: bytes) -> bytes:
    """The whole child as a function of its stdin: the stdout bytes. `BadRequest` propagates."""
    try:
        request = json.loads(stdin)
    except ValueError:
        raise BadRequest("json") from None
    if not isinstance(request, dict) or request.get("op") not in OPS:
        raise BadRequest("op")
    op = request["op"]
    inputs = request.get("inputs") or {}
    params = request.get("params") or {}
    if not isinstance(inputs, Mapping) or not isinstance(params, Mapping):
        raise BadRequest("inputs or params")
    if op == "prepare":
        reply = _prepare(inputs)
    elif op == "finish_white":
        reply = _finish_white(inputs, params)
    elif op == "fix":
        reply = _fix(inputs, params)
    else:
        reply = _size_chart(params)
    return json.dumps(reply, separators=(",", ":")).encode("ascii")


def main() -> int:
    set_limits()
    try:
        out = run(sys.stdin.buffer.read())
    except BadRequest:
        return EXIT_BAD_REQUEST
    sys.stdout.buffer.write(out)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
