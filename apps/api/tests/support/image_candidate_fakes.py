"""A test-only stand-in for `app.services.image_candidate_child` (SI7 T8), selected through the
runner's `CANDIDATE_MODULE`. The request's `params.fake` picks the behaviour. Never imported by
the app."""

from __future__ import annotations

import json
import sys
import time


def main() -> int:
    request = json.loads(sys.stdin.buffer.read())
    mode = request.get("params", {}).get("fake")
    if mode == "hang":
        time.sleep(30)
        return 0
    if mode == "crash":
        sys.stderr.write("CANARY-STDERR the decoder said something about the image\n")
        return 3
    if mode == "flood":
        chunk = b"A" * 65536
        for _ in range(800):  # 50 MiB
            sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()
        return 0
    if mode == "garbage":
        sys.stdout.write("not json at all")
        return 0
    if mode == "bad_reason":
        sys.stdout.write(json.dumps({"ok": False, "reason": "model_failed"}))
        return 0
    return 9


if __name__ == "__main__":
    sys.exit(main())
