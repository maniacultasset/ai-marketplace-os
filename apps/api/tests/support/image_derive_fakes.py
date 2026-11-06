"""A test-only stand-in for `app.services.image_derive_child` (SI1 T15), selected through the
runner's `DERIVE_MODULE`. The runner passes no argv, so the first byte of stdin picks the
behaviour. Never imported by the app."""

from __future__ import annotations

import os
import sys
import time


def main() -> int:
    data = sys.stdin.buffer.read()
    mode = data[:1]
    if mode == b"B":  # far more stdout than the cap allows
        chunk = b"A" * 65536
        for _ in range(int(data[1:] or b"64")):
            sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()
        return 0
    if mode == b"S":  # sleeps past any test timeout
        time.sleep(30)
        return 0
    if mode == b"X":  # killed, as by the OOM killer
        os._exit(137)
    if mode == b"G":  # garbage stdout
        sys.stdout.write("not json at all")
        return 0
    sys.stdout.write("null")
    return 0


if __name__ == "__main__":
    sys.exit(main())
