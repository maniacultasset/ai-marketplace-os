"""Supports AC11/AC15: every CI shell script parses and fails fast."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from tests.meta.test_compose import canon_local_bucket_names


def _scripts(repo_root: Path) -> list[Path]:
    scripts = sorted((repo_root / "scripts/ci").glob("*.sh"))
    assert scripts
    return scripts


def test_prove_gates_has_misspelled_bucket_leg(repo_root: Path) -> None:
    text = (repo_root / "scripts" / "ci" / "prove_gates.sh").read_text(encoding="utf-8")
    assert re.search(r"bucket marketplace-csv missing", text)
    bootstrap_legs = [m.start() for m in re.finditer(r"\bleg bootstrap", text)]
    assert len(bootstrap_legs) >= 2
    between = text[bootstrap_legs[0] : bootstrap_legs[1]]
    assert "down -v" in between
    assert "prove-gates-bootstrap" in between
    assert "cp .env.example .env" in text
    assert "seed_misspelled_bucket" in text


def test_shell_scripts_parse(repo_root: Path) -> None:
    for script in _scripts(repo_root):
        result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert result.returncode == 0, (script.name, result.stderr)


def test_bootstrap_checks_minio_init_and_both_buckets(repo_root: Path) -> None:
    text = (repo_root / "scripts" / "ci" / "bootstrap.sh").read_text(encoding="utf-8")
    assert "ps -a --format json minio-init" in text
    assert '"exited"' in text
    assert "ExitCode" in text
    match = re.search(r"buckets=\(([^)]+)\)", text)
    assert match is not None
    names = match.group(1).split()
    assert names == canon_local_bucket_names(repo_root)
    assert re.search(r"run --rm --no-deps minio-init ls", text)
    for forbidden in ("--entrypoint", "S3_SECRET_KEY", "S3_ACCESS_KEY", "MC_HOST"):
        assert not any(forbidden in line for line in text.splitlines()), forbidden
    up_at = text.index("up -d")
    uvicorn_at = text.index("uvicorn")
    init_at = text.index("minio-init")
    assert up_at < init_at < uvicorn_at


def test_shell_scripts_fail_fast(repo_root: Path) -> None:
    for script in _scripts(repo_root):
        lines = script.read_text(encoding="utf-8").splitlines()
        assert lines[0] == "#!/usr/bin/env bash", script.name
        assert "set -euo pipefail" in lines[:15], script.name
