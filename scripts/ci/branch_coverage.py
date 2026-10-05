"""Branch-coverage gate for one package (ADR-0010 §2: core modules need 90% branch coverage).

`coverage report --fail-under` gates line and branch coverage combined, so this script reads the
totals of a `coverage json` report (already narrowed with `--include`) and fails when
covered_branches / num_branches is below `--min`, or when the selection has no branches (a wrong
glob must never pass). Part of `make api-tests`. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def branch_percent(report: object) -> tuple[float, int]:
    """(percent, number of branches) from a `coverage json` report."""
    if not isinstance(report, dict) or not isinstance(report.get("totals"), dict):
        raise ValueError("not a coverage json report: no totals")
    totals = report["totals"]
    branches = totals.get("num_branches")
    covered = totals.get("covered_branches")
    if not isinstance(branches, int) or not isinstance(covered, int):
        raise ValueError("coverage json has no branch totals (was branch coverage on?)")
    if branches == 0:
        return 0.0, 0
    return 100.0 * covered / branches, branches


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--min", type=float, required=True, dest="minimum")
    parser.add_argument("--label", required=True)
    args = parser.parse_args(argv)
    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
        percent, branches = branch_percent(report)
    except (OSError, ValueError) as exc:
        print(f"{args.label} branch coverage: unreadable report: {exc}")
        return 1
    print(f"{args.label} branch coverage {percent:.1f}% (min {args.minimum:g})")
    if branches == 0:
        print(f"{args.label}: no branches measured; check the --include glob")
        return 1
    return 0 if percent >= args.minimum else 1


if __name__ == "__main__":
    sys.exit(main())
