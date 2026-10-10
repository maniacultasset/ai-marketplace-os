"""The nightly's skip rule (S26 T24; AC 12; PLAN D8): a heavy leg skips when `main` has had no
commit in the last 24 hours, unless the run is forced (`workflow_dispatch` input `force`).

    python3 scripts/ci/nightly_skip.py <last-commit-epoch> [--force true|false] [--now EPOCH]

Exit 0 and prints `SKIP main unchanged in 24 h` when the leg should skip; exit 1 (prints `RUN`)
when it should run. Stdlib only; the decision is the pure `should_skip`.
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Final

WINDOW_S: Final = 24 * 60 * 60
SKIP_LINE: Final = "SKIP main unchanged in 24 h"


def should_skip(last_commit: float, now: float, force: bool) -> bool:
    """True when the leg skips: not forced, and the newest commit is 24 h or more old. A commit
    time in the future (clock skew) never skips."""
    if force:
        return False
    return now - last_commit >= WINDOW_S


def parse_force(raw: str | None) -> bool:
    return (raw or "").strip().lower() == "true"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts/ci/nightly_skip.py")
    parser.add_argument("last_commit", type=float)
    parser.add_argument("--force", default="false")
    parser.add_argument("--now", type=float, default=None)
    args = parser.parse_args(argv)
    now = time.time() if args.now is None else args.now
    if should_skip(args.last_commit, now, parse_force(args.force)):
        print(SKIP_LINE)
        return 0
    print("RUN")
    return 1


if __name__ == "__main__":
    sys.exit(main())
