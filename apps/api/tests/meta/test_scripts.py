"""Supports AC11/AC15: every CI shell script parses and fails fast.

`test_bootstrap_checks_minio_init_and_both_buckets` lands with the MinIO services (DEVIATIONS.md).
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def _scripts(repo_root: Path) -> list[Path]:
    scripts = sorted((repo_root / "scripts/ci").glob("*.sh"))
    assert scripts
    return scripts


def test_shell_scripts_parse(repo_root: Path) -> None:
    for script in _scripts(repo_root):
        result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert result.returncode == 0, (script.name, result.stderr)


def test_shell_scripts_fail_fast(repo_root: Path) -> None:
    for script in _scripts(repo_root):
        lines = script.read_text(encoding="utf-8").splitlines()
        assert lines[0] == "#!/usr/bin/env bash", script.name
        assert "set -euo pipefail" in lines[:15], script.name
