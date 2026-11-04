"""DW2 decision 4 (AC7): only `app.cli` reads the platform refresh token setting. The api and
the worker never touch it (they also refuse to boot with it set outside local), so a platform
authorization can never stand in for a tenant token in a request or a job."""

from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"
SETTING = "amazon_platform_refresh_token"
ALLOWED = {"cli.py", "core/config.py"}  # the reader, and the field's own definition


def readers(root: Path = APP) -> set[str]:
    found: set[str] = set()
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and node.attr == SETTING) or (
                isinstance(node, ast.Constant) and node.value == SETTING
            ):
                found.add(path.relative_to(root).as_posix())
    return found


def test_only_the_cli_reads_the_platform_refresh_token() -> None:
    assert readers() - ALLOWED == set()
    assert "cli.py" in readers()


def test_the_walk_catches_a_worker_reader(tmp_path: Path) -> None:
    (tmp_path / "workers").mkdir()
    (tmp_path / "workers" / "adapters.py").write_text(
        f"def f(s):\n    return s.{SETTING}\n", encoding="utf-8"
    )
    assert readers(tmp_path) == {"workers/adapters.py"}
