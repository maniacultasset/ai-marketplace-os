"""The image check child process (S17b PLAN D6, CP6): `python -m app.services.image_check_child`.

The worker never decodes an uploaded image in its own process. It starts this module with an
environment allow-list (no DSN, JWT, KMS or S3 secret), writes one JSON header line and then
the raw bytes to stdin, and reads one JSON object from stdout:

- stdin: `{"image_id", "slot", "now", "rules": {marketplace: [Rule, ...]}}` + `\\n` + bytes;
- stdout: `{"reports": {marketplace: ImageReport}, "meta": {mime_type, width_px, height_px} | null}`;
- exit 0 on success, 4 when a rule is misconfigured (S17a raises `ValueError`; no stdout), 2 on
  a malformed header. Anything else (a crash, a signal, a timeout) the runner treats as CORRUPT.

On Linux it first caps its own CPU time and address space (`RLIMIT_CPU`, `RLIMIT_AS`); on macOS
those limits are not enforced and are skipped. It imports only the pure S17a checks,
`mkos_schemas`, Pillow and the stdlib (import contract `image-check-child-is-pure`); it is not a
credential sandbox (same uid and network as the worker, CP6).
"""

from __future__ import annotations

import io
import json
import sys
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from mkos_schemas.enums import ImageSlot, Marketplace
from mkos_schemas.rules import Rule
from PIL import Image

from app.services.image_checks import DECODE_FORMATS, check_image

CPU_LIMIT_S: Final = 60
AS_LIMIT_BYTES: Final = 1024 * 1024 * 1024
EXIT_BAD_HEADER: Final = 2
EXIT_RULE_MISCONFIGURED: Final = 4
MAX_HEADER_BYTES: Final = 4 * 1024 * 1024


def set_limits() -> None:
    """Cap CPU seconds and the address space (Linux); a named no-op elsewhere."""
    if not sys.platform.startswith("linux"):
        return
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (CPU_LIMIT_S, CPU_LIMIT_S))
    resource.setrlimit(resource.RLIMIT_AS, (AS_LIMIT_BYTES, AS_LIMIT_BYTES))


def probe_meta(data: bytes) -> dict[str, Any] | None:
    """Type and size from the header only (no pixel load); None when Pillow cannot open it."""
    try:
        with Image.open(io.BytesIO(data), formats=list(DECODE_FORMATS)) as img:
            mime = Image.MIME.get(img.format or "")
            width, height = img.size
    except Exception:
        return None
    if mime is None or width < 1 or height < 1:
        return None
    return {"mime_type": mime, "width_px": width, "height_px": height}


def run(stdin: bytes) -> tuple[int, bytes]:
    """The whole child as a function of its stdin: (exit code, stdout bytes)."""
    header, sep, data = stdin.partition(b"\n")
    if not sep or len(header) > MAX_HEADER_BYTES:
        return EXIT_BAD_HEADER, b""
    try:
        spec = json.loads(header)
        image_id = UUID(spec["image_id"])
        slot = ImageSlot(spec["slot"])
        now = datetime.fromisoformat(spec["now"])
        rules = {
            Marketplace(m): [Rule.model_validate(r) for r in rows]
            for m, rows in spec["rules"].items()
        }
    except (ValueError, KeyError, TypeError):
        return EXIT_BAD_HEADER, b""
    reports: dict[str, Any] = {}
    try:
        for market, market_rules in rules.items():
            report = check_image(
                data, image_id=image_id, marketplace=market, slot=slot, rules=market_rules, now=now
            )
            reports[market.value] = report.model_dump(mode="json", by_alias=True)
    except ValueError:
        return EXIT_RULE_MISCONFIGURED, b""
    out = {"reports": reports, "meta": probe_meta(data)}
    return 0, json.dumps(out, separators=(",", ":")).encode("utf-8")


def main() -> int:
    set_limits()
    code, out = run(sys.stdin.buffer.read())
    if out:
        sys.stdout.buffer.write(out)
        sys.stdout.buffer.flush()
    return code


if __name__ == "__main__":
    sys.exit(main())
