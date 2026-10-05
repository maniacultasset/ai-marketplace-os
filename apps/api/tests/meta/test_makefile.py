"""Every `uv run` is frozen, and `make hooks` is the install path (P19)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

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


# ---- S5: the Alembic round trip and squawk before pytest (CANON.md:136); `make dev` upgrades ----


def _dry_run(repo_root: Path, target: str) -> list[str]:
    import os
    import subprocess

    env = {**os.environ, "CI": "true"}  # CI skips the compose port guard
    result = subprocess.run(
        ["make", "-n", "-C", str(repo_root), target],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return result.stdout.splitlines()


def _first(lines: list[str], needle: str, start: int = 0) -> int:
    for index in range(start, len(lines)):
        if needle in lines[index]:
            return index
    raise AssertionError(f"{needle!r} not found after line {start}: {lines}")


def test_api_tests_runs_alembic_sequence_before_pytest(repo_root: Path) -> None:
    lines = _dry_run(repo_root, "api-tests")
    alembic = "alembic -c apps/api/alembic.ini"
    at = _first(lines, f"{alembic} upgrade head")
    assert "--sql" not in lines[at]
    at = _first(lines, f"{alembic} check", at + 1)
    at = _first(lines, f"{alembic} downgrade base", at + 1)
    at = _first(lines, f"{alembic} upgrade head", at + 1)
    assert "--sql" not in lines[at]
    at = _first(lines, f"{alembic} upgrade head --sql", at + 1)
    at = _first(lines, "squawk_report.py", at)  # the same shell line, after the render
    _first(lines, "pytest", at + 1)


def test_api_tests_sets_alembic_url_itself(repo_root: Path, load_yaml: Any) -> None:
    lines = _dry_run(repo_root, "api-tests")
    alembic_lines = [line for line in lines if "alembic -c apps/api/alembic.ini" in line]
    assert len(alembic_lines) == 5
    for line in alembic_lines:
        assert "ALEMBIC_DATABASE_URL=postgresql+psycopg://" in line
        assert re.search(r"ALEMBIC_DATABASE_URL=\S+/mos_test\b", line), line
    workflow = load_yaml(repo_root / ".github/workflows/ci-api.yml")
    for job_id, job in workflow["jobs"].items():
        env = job.get("env")
        if env is not None:
            assert sorted(env) == ["APP_ENV", "DATABASE_URL", "JWT_SECRET"], job_id


def test_dev_upgrades_after_bootstrap_db_and_sync(repo_root: Path) -> None:
    text = (repo_root / "Makefile").read_text(encoding="utf-8")
    dev = re.search(r"^dev:\n((?:\t.+\n)+)", text, re.MULTILINE)
    assert dev is not None
    body = dev.group(1)
    bootstrap = body.index("$(MAKE) bootstrap-db")
    sync = body.index("uv sync --frozen --all-packages")
    upgrade = body.index("uv run --frozen alembic -c apps/api/alembic.ini upgrade head")
    serve = body.index("uvicorn")
    assert bootstrap < sync < upgrade < serve
