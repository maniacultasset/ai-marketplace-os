"""Every `uv run` is frozen, and `make hooks` is the install path (P19)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_BARE_UV_RUN = re.compile(r"\buv\s+run\b(?!\s+--frozen(?:\s|$))")


def bare_uv_runs(text: str) -> list[int]:
    return [i for i, line in enumerate(text.splitlines(), start=1) if _BARE_UV_RUN.search(line)]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("uv run pytest", [1]),
        ("\tuv  run mypy", [1]),
        ("uv run --frozen-lockfile x", [1]),
        ("x && uv run y", [1]),
        ("uv run --frozen ruff", []),
        ("uv sync --frozen", []),
        ("", []),
        ("\n", []),
    ],
)
def test_bare_uv_run_detector_flags_seeded_lines(text: str, expected: list[int]) -> None:
    assert bare_uv_runs(text) == expected


def test_every_uv_run_is_frozen(repo_root: Path) -> None:
    makefile = (repo_root / "Makefile").read_text(encoding="utf-8")
    assert bare_uv_runs(makefile) == []
    for script in sorted((repo_root / "scripts" / "ci").glob("*.sh")):
        assert bare_uv_runs(script.read_text(encoding="utf-8")) == [], script


def test_hooks_target_installs_and_dev_calls_it(repo_root: Path) -> None:
    text = (repo_root / "Makefile").read_text(encoding="utf-8")
    assert re.search(r"^\.PHONY:\s+hooks\s*$", text, re.MULTILINE)
    hooks = re.search(r"^hooks:\n\t(.+)\n", text, re.MULTILINE)
    assert hooks is not None
    assert hooks.group(1) == "uv run --frozen pre-commit install"
    dev = re.search(r"^dev:\n((?:\t.+\n)+)", text, re.MULTILINE)
    assert dev is not None
    body = dev.group(1)
    assert "uv sync --frozen --all-packages" in body
    sync_at = body.index("uv sync --frozen --all-packages")
    assert "$(MAKE) hooks" in body[sync_at:]
    assert "pre-commit install" not in body


def test_help_lists_hooks(repo_root: Path) -> None:
    text = (repo_root / "Makefile").read_text(encoding="utf-8")
    help_echo = next(line for line in text.splitlines() if "make dev" in line and "echo" in line)
    assert "hooks" in help_echo
