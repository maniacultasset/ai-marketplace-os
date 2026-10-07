"""A test-only stand-in for `app.services.image_check_child` (S17b T6), selected through the
runner's `CHILD_MODULE`. The runner passes no argv, so the behaviour comes from the first eight
hex digits of the header's `image_id`, which each test chooses. Never imported by the app."""

from __future__ import annotations

import json
import os
import sys
import time


def main() -> int:
    raw = sys.stdin.buffer.read()
    header = json.loads(raw.partition(b"\n")[0])
    mode = header["image_id"][:8]
    if mode == "00000001":  # slow: sleeps past any test timeout
        time.sleep(30)
        return 0
    if mode == "00000002":  # crash
        sys.stderr.write("CANARY-STDERR decoder said something about the image\n")
        return 3
    if mode == "00000003":  # garbage stdout
        sys.stdout.write("not json at all")
        return 0
    if mode == "00000004":  # env dump (keys only)
        sys.stdout.write(json.dumps(sorted(os.environ)))
        return 0
    if mode == "00000005":  # half a second of work, then a crash (the concurrency test)
        time.sleep(0.5)
        return 3
    if mode == "00000006":  # a valid shape missing one marketplace
        report = {
            "image_id": header["image_id"],
            "marketplace": "amazon",
            "slot": header["slot"],
            "pass": True,
            "checks": [],
            "issues": [],
            "processed_at": header["now"],
        }
        sys.stdout.write(json.dumps({"reports": {"amazon": report}, "meta": None}))
        return 0
    return 9


if __name__ == "__main__":
    sys.exit(main())
