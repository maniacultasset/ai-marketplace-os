"""S26 T24 (AC 12; PLAN D8): scripts/ci/nightly_skip.py, the heavy legs' skip rule."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPO_ROOT / "scripts/ci/nightly_skip.py"
DAY = 24 * 60 * 60
NOW = 1_791_600_000.0


def _module() -> ModuleType:
    if "nightly_skip" in sys.modules:
        return sys.modules["nightly_skip"]
    spec = importlib.util.spec_from_file_location("nightly_skip", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["nightly_skip"] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("age_s", "force", "skip"),
    [
        (60, False, False),  # a commit a minute ago: run
        (DAY - 1, False, False),  # just inside the window: run
        (DAY, False, True),  # exactly 24 h: skip
        (3 * DAY, False, True),
        (3 * DAY, True, False),  # forced: always run
        (-600, False, False),  # a commit "in the future" (skew): run
    ],
)
def test_should_skip(age_s: int, force: bool, skip: bool) -> None:
    assert _module().should_skip(NOW - age_s, NOW, force) is skip


@pytest.mark.parametrize(
    ("raw", "force"),
    [("true", True), ("TRUE", True), ("false", False), ("", False), (None, False), ("yes", False)],
)
def test_parse_force(raw: str | None, force: bool) -> None:
    assert _module().parse_force(raw) is force


def test_cli_prints_the_skip_line() -> None:
    old = str(NOW - 2 * DAY)
    skip = subprocess.run(
        [sys.executable, str(SCRIPT), old, "--now", str(NOW)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert (skip.returncode, skip.stdout.strip()) == (0, "SKIP main unchanged in 24 h")
    forced = subprocess.run(
        [sys.executable, str(SCRIPT), old, "--now", str(NOW), "--force", "true"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert (forced.returncode, forced.stdout.strip()) == (1, "RUN")
