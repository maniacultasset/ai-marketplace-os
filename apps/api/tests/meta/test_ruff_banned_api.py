"""AC5: ruff `TID` enforces the banned APIs (ADR-0013 §3; KNOWN-ISSUES.md:35; CANON.md:126)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

BANNED = ("asyncpg", "redis", "arq", "jose", "passlib", "numpy", "openai", "langgraph")


def _ruff(repo_root: Path, filename: str, source: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--no-cache", "--stdin-filename", filename, "-"],
        input=source,
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("module", BANNED)
def test_banned_modules_flagged(repo_root: Path, module: str) -> None:
    result = _ruff(repo_root, "apps/api/app/x.py", f"import {module}\n\nUSED = {module}\n")
    assert result.returncode == 1
    assert "TID251" in result.stdout


@pytest.mark.parametrize("filename", ["embeddings.py", "vision.py"])
def test_openai_allowed_in_sanctioned_modules(repo_root: Path, filename: str) -> None:
    source = "import openai\n\nCLIENT = openai\n"
    allowed = _ruff(repo_root, f"apps/api/app/services/{filename}", source)
    assert allowed.returncode == 0, allowed.stdout
    banned = _ruff(repo_root, "apps/api/app/services/rules_engine.py", source)
    assert "TID251" in banned.stdout
