"""The per-module mutation score (S26 T23; AC 11, DW6; PLAN D9): `make mutation` runs it after
`mutmut run`.

    python scripts/ci/mutation_report.py [--root apps/api] [--waivers docs/review/MUTATION-WAIVERS.md]

Reads mutmut 3.8.0's results, one `mutants/<path>.meta` JSON file per mutated source file under
`--root` (`exit_code_by_key`: mutant -> the test run's exit code, mutmut's `status_by_exit_code`),
and groups them by the core modules `[tool.mutmut] only_mutate` names in `<root>/pyproject.toml`
(a `<dir>/*` entry is one module, the package). A mutant is detected when it was killed (exit 1 or
3), timed out (24, 36, 152, 255, -24) or caught by the type check (37); a skipped one (34) is not
counted; every other status (survived 0, no tests 5 or 33, suspicious, segfault) counts against
the module. The score is detected / counted. A module fails under 80% unless
docs/review/MUTATION-WAIVERS.md has its row (module | release | score | reason | user | date,
the run's score at least the waived one); a module with no mutants, a mutant not checked yet
(None) or a malformed waiver row fails the run. Prints one line per module and exits 1 on any
failure. Stdlib only.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import sys
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Final

THRESHOLD: Final = 80.0
DETECTED: Final = frozenset({1, 3, 24, 36, 37, 152, 255, -24})
SKIPPED: Final = frozenset({34})
WAIVER_COLUMNS: Final = ("module", "release", "score", "reason", "user", "date")


@dataclass
class Score:
    module: str
    detected: int = 0
    total: int = 0
    skipped: int = 0
    unchecked: int = 0

    @property
    def percent(self) -> float:
        return 100.0 * self.detected / self.total if self.total else 0.0


@dataclass(frozen=True)
class Waiver:
    module: str
    release: str
    score: float
    reason: str
    user: str
    day: date


class WaiverError(ValueError):
    """A waivers row that does not parse; the message names the line."""


def module_of(path: str, modules: Iterable[str]) -> str | None:
    """The core module a mutated file belongs to: `<dir>/*` -> `<dir>`, else the file itself."""
    for entry in modules:
        if fnmatch.fnmatch(path, entry):
            return entry[:-2] if entry.endswith("/*") else entry
    return None


def module_names(modules: Iterable[str]) -> list[str]:
    return [m[:-2] if m.endswith("/*") else m for m in modules]


def scores(root: Path, modules: list[str]) -> dict[str, Score]:
    out = {name: Score(name) for name in module_names(modules)}
    mutants = root / "mutants"
    for meta in sorted(mutants.rglob("*.py.meta")) if mutants.is_dir() else []:
        path = meta.relative_to(mutants).as_posix().removesuffix(".meta")
        name = module_of(path, modules)
        if name is None:
            continue
        data = json.loads(meta.read_text(encoding="utf-8"))
        score = out[name]
        for code in data["exit_code_by_key"].values():
            if code is None:
                score.unchecked += 1
            elif code in SKIPPED:
                score.skipped += 1
            else:
                score.total += 1
                score.detected += code in DETECTED
    return out


def load_waivers(path: Path, known: Iterable[str] | None) -> dict[str, Waiver]:
    """The waiver rows of the markdown table (header and separator lines skipped)."""
    if not path.is_file():
        return {}
    allowed = None if known is None else set(known)
    waivers: dict[str, Waiver] = {}
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if tuple(cells) == WAIVER_COLUMNS or all(set(c) <= {"-", ":"} for c in cells):
            continue
        if len(cells) != len(WAIVER_COLUMNS) or not all(cells):
            raise WaiverError(f"waiver line {n}: want {len(WAIVER_COLUMNS)} non-empty cells")
        module, release, raw_score, reason, user, raw_day = cells
        if allowed is not None and module not in allowed:
            raise WaiverError(f"waiver line {n}: {module} is not a core module")
        try:
            score = float(raw_score)
        except ValueError:
            raise WaiverError(f"waiver line {n}: score {raw_score!r} is not a number") from None
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_day):
            raise WaiverError(f"waiver line {n}: date {raw_day!r} is not YYYY-MM-DD")
        try:
            day = date.fromisoformat(raw_day)
        except ValueError:
            raise WaiverError(f"waiver line {n}: date {raw_day!r} is not a date") from None
        waivers[module] = Waiver(module, release, score, reason, user, day)
    return waivers


def configured_modules(root: Path) -> list[str]:
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    return list(config["tool"]["mutmut"]["only_mutate"])


def run(
    root: Path,
    modules: list[str],
    waivers_path: Path,
    *,
    out: Callable[[str], None] = print,
) -> int:
    failed = False
    try:
        waivers = load_waivers(waivers_path, module_names(modules))
    except WaiverError as exc:
        out(f"mutation: FAILED malformed waiver ({exc})")
        return 1
    for name, score in scores(root, modules).items():
        label = f"mutation: {name} {score.detected}/{score.total} {score.percent:.1f}%"
        if score.skipped:
            label += f" ({score.skipped} skipped)"
        if score.unchecked:
            out(f"{label} FAILED {score.unchecked} mutants not checked (run mutmut to the end)")
            failed = True
        elif score.total == 0:
            out(f"mutation: {name} FAILED no mutants (an only_mutate entry matches nothing)")
            failed = True
        elif score.percent >= THRESHOLD:
            out(f"{label} ok")
        elif name in waivers and score.percent >= waivers[name].score:
            w = waivers[name]
            out(f"{label} waived ({w.release}, {w.score:.1f}%, {w.user} {w.day.isoformat()})")
        else:
            out(f"{label} FAILED under {THRESHOLD:.0f}% with no waiver")
            failed = True
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts/ci/mutation_report.py")
    parser.add_argument("--root", type=Path, default=Path("apps/api"))
    parser.add_argument("--waivers", type=Path, default=Path("docs/review/MUTATION-WAIVERS.md"))
    args = parser.parse_args(argv)
    return run(args.root, configured_modules(args.root), args.waivers)


if __name__ == "__main__":
    sys.exit(main())
