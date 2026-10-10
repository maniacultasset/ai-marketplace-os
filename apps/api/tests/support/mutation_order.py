"""The test order of one mutmut child (`make mutation`, S26 T23): unit files before integration
files, the faster files first (by the durations of mutmut's own stats run, `mutmut-stats.json`
in the mutants copy), each file's tests kept in their collected order. mutmut runs a mutant's
tests with `-x`, so a killed mutant stops at its first failing test; running the cheap files
first ends most runs in a fraction of a second. Which tests run never changes, only their order.

Nothing here imports `app`."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol


class _Item(Protocol):
    @property
    def nodeid(self) -> str: ...


def _durations(stats: Path) -> dict[str, float]:
    try:
        raw: Any = json.loads(stats.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    found = raw.get("duration_by_test") if isinstance(raw, dict) else None
    if not isinstance(found, dict):
        return {}
    return {str(k): float(v) for k, v in found.items() if isinstance(v, (int, float))}


def _file(nodeid: str) -> str:
    return nodeid.split("::", 1)[0]


def mutation_order[T: _Item](items: Sequence[T], stats: Path) -> list[T]:
    durations = _durations(stats)
    files: dict[str, list[T]] = {}
    for item in items:
        files.setdefault(_file(item.nodeid), []).append(item)

    def key(path: str) -> tuple[bool, float, str]:
        cost = sum(durations.get(item.nodeid, 0.0) for item in files[path])
        return ("integration" in Path(path).parts, cost, path)

    return [item for path in sorted(files, key=key) for item in files[path]]
