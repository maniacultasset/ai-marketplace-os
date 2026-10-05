"""squawk over the SQL Alembic renders (`upgrade head --sql`), report-only until the first prod tag
(EXECUTION-PLAN.md:303-305). Part of `make api-tests`. Stdlib only.

- Every finding is printed, and the rules DATABASE.md §8.3 lists are tagged; strict mode (S25)
  fails on the tagged ones. squawk has no "only these rules" setting, so the list lives here.
- squawk exits 1 both for findings and for its own failures, so its JSON report tells them
  apart. A missing binary, another exit code, output that is not a JSON list, or a
  `syntax-error` finding (squawk could not parse the SQL, so it checked nothing) fails closed.
- Only the lint command runs: never squawk's GitHub upload or its LSP mode.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
DEFAULT_SQUAWK: Final = "squawk"
TAG: Final = "[DATABASE.md §8.3]"
DATABASE_MD_RULES: Final = (
    "require-concurrent-index-creation",
    "constraint-missing-not-valid",
    "adding-foreign-key-constraint",
    "adding-not-nullable-field",
    "changing-column-type",
    "renaming-column",
    "ban-drop-column",
    "prefer-timestamp-tz",
    "prefer-robust-stmts",
)
TIMEOUT_S: Final = 120


class SquawkToolError(Exception):
    """squawk did not check the SQL."""


def squawk_argv(squawk: str, sql_path: Path) -> list[str]:
    return [squawk, "--reporter", "json", "--pg-version", "17", str(sql_path)]


def _findings(stdout: str) -> list[dict[str, object]]:
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise SquawkToolError("output is not JSON") from exc
    if not isinstance(parsed, list) or not all(isinstance(item, dict) for item in parsed):
        raise SquawkToolError("output is not a JSON list of findings")
    return parsed


def _line(finding: dict[str, object]) -> str:
    rule = str(finding.get("rule_name", "?"))
    line = finding.get("line")
    where = f"{finding.get('file', '?')}:{line + 1 if isinstance(line, int) else '?'}"
    message = " ".join(str(finding.get("message", "")).split())
    tag = f" {TAG}" if rule in DATABASE_MD_RULES else ""
    return f"squawk: {where} {rule}{tag} {message}"


def _run(sql_path: Path, squawk: str) -> list[dict[str, object]]:
    try:
        result = subprocess.run(
            squawk_argv(squawk, sql_path),
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
            check=False,
            cwd=REPO_ROOT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SquawkToolError(type(exc).__name__) from exc
    if result.returncode == 0:
        return []
    if result.returncode != 1:
        raise SquawkToolError(f"exit {result.returncode}")
    findings = _findings(result.stdout)
    if not findings:
        raise SquawkToolError("exit 1 without findings")
    if any(f.get("rule_name") == "syntax-error" for f in findings):
        raise SquawkToolError("syntax-error: squawk could not parse the SQL")
    return findings


def report(sql_path: Path, squawk: str = DEFAULT_SQUAWK) -> int:
    try:
        findings = _run(sql_path, squawk)
    except SquawkToolError as exc:
        print(f"squawk_report: squawk did not check the SQL ({exc})", file=sys.stderr)
        return 1
    if not findings:
        print("squawk: 0 findings")
        return 0
    for finding in findings:
        print(_line(finding))
    listed = sum(1 for f in findings if f.get("rule_name") in DATABASE_MD_RULES)
    print(
        f"squawk: {len(findings)} findings ({listed} on DATABASE.md §8.3 rules); "
        "report-only until the first prod tag"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("sql", type=Path, help="the SQL file `alembic upgrade head --sql` wrote")
    args = parser.parse_args(argv)
    return report(args.sql, squawk=DEFAULT_SQUAWK)


if __name__ == "__main__":
    raise SystemExit(main())
