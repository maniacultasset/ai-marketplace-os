"""Wall-clock budgets that mean the same thing on a laptop and on a shared CI runner.

A budget is written in laptop seconds (an untraced run on the machine the test was tuned on).
`budget(seconds)` scales it by how much slower this machine runs a fixed reference workload, and
by the coverage tracer's overhead when `make api-tests` runs under `--cov` (measured: the engine
legs take 1.5-1.9 s traced against 0.75-0.8 s untraced). The regressions these tests guard are
10-80x, so a scaled budget still catches them.
"""

from __future__ import annotations

import functools
import itertools
import re
import time

import coverage

# The reference workload's best-of-3 time on the tuning machine, untraced.
_REFERENCE_SECONDS = 0.036
_TRACED_FACTOR = 2.5


_runs = itertools.count()


def _reference_workload() -> float:
    run = next(_runs)
    start = time.perf_counter()
    total = 0
    for i in range(300_000):
        total += len(str(i)) & 3
    # sre's parser and compiler are Python; a run-unique suffix keeps the re cache out of it
    for i in range(400):
        re.compile(f"(?:[a-{chr(0x100 + i)}]|x{i})+y{run}", re.IGNORECASE)
    return time.perf_counter() - start


@functools.cache
def machine_factor() -> float:
    return max(1.0, min(_reference_workload() for _ in range(3)) / _REFERENCE_SECONDS)


def budget(seconds: float) -> float:
    traced = _TRACED_FACTOR if coverage.Coverage.current() is not None else 1.0
    return seconds * machine_factor() * traced
