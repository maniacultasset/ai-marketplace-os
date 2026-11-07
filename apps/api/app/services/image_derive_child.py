"""The image derive child process (SI1 T15, CP17): `python -m app.services.image_derive_child`.

A second child, after the checks, so a derive crash, kill or timeout never touches the check
report. The worker starts it with the same environment allow-list, writes the raw image bytes to
stdin and reads one JSON value from stdout:

- `{"clean_b64", "clean_mime", "vision_b64", "vision_w", "vision_h", "border"}` (SI7: the border
  profile `{uniform, near_white, colour}`), or
- `null` when the image yields no derivatives (exit 0 either way).

It caps its own CPU time and address space like the check child (Linux only) and imports only
`app.services.image_derive`, Pillow and the stdlib (import contract `image-check-child-is-pure`).
"""

from __future__ import annotations

import base64
import json
import sys

from app.services.image_check_child import set_limits
from app.services.image_derive import derive


def run(stdin: bytes) -> bytes:
    """The whole child as a function of its stdin: the stdout bytes."""
    found = derive(stdin)
    if found is None:
        return b"null"
    body = {
        "clean_b64": base64.b64encode(found.clean).decode("ascii"),
        "clean_mime": found.clean_mime,
        "vision_b64": base64.b64encode(found.vision).decode("ascii"),
        "vision_w": found.vision_w,
        "vision_h": found.vision_h,
        "border": found.border,
    }
    return json.dumps(body, separators=(",", ":")).encode("ascii")


def main() -> int:
    set_limits()
    out = run(sys.stdin.buffer.read())
    sys.stdout.buffer.write(out)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
