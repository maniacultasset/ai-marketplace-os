"""S14a T14: scripts/ci/branch_coverage.py, the per-package branch gate (ADR-0010 §2; D11)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = "scripts/ci/branch_coverage.py"


def _run(
    repo_root: Path, tmp_path: Path, payload: object | str
) -> subprocess.CompletedProcess[str]:
    report = tmp_path / "coverage.json"
    text = payload if isinstance(payload, str) else json.dumps(payload)
    report.write_text(text, encoding="utf-8")
    return subprocess.run(
        [sys.executable, SCRIPT, str(report), "--min", "90", "--label", "rules_engine"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )


def _totals(covered: int, branches: int) -> dict[str, object]:
    return {"totals": {"covered_branches": covered, "num_branches": branches}}


def test_branch_coverage_passes_at_min(repo_root: Path, tmp_path: Path) -> None:
    result = _run(repo_root, tmp_path, _totals(90, 100))
    assert result.returncode == 0, result.stdout
    assert "rules_engine branch coverage 90.0% (min 90)" in result.stdout


def test_branch_coverage_fails_below(repo_root: Path, tmp_path: Path) -> None:
    result = _run(repo_root, tmp_path, _totals(89, 100))
    assert result.returncode == 1
    assert "rules_engine branch coverage 89.0% (min 90)" in result.stdout


def test_branch_coverage_fails_zero_branches(repo_root: Path, tmp_path: Path) -> None:
    result = _run(repo_root, tmp_path, _totals(0, 0))
    assert result.returncode == 1
    assert "no branches measured" in result.stdout


def test_branch_coverage_bad_json_fails(repo_root: Path, tmp_path: Path) -> None:
    for payload in ["not json", [], {"totals": {}}, {"files": {}}]:
        result = _run(repo_root, tmp_path, payload)
        assert result.returncode == 1, payload
        assert "unreadable report" in result.stdout
