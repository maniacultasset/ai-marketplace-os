"""S9 T12 (AC 5, 6; A§5, D12, D13, D16): the `seed` sub-command's arguments, the fixture path,
the deterministic tenant ids, and a CLI module free of `assert` and `print`."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest
from app import cli


def test_seed_requires_profile_exit_2(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        cli.main(["seed"])
    assert info.value.code == 2
    assert "--profile" in capsys.readouterr().err


def test_seed_unknown_profile_exit_2() -> None:
    with pytest.raises(SystemExit) as info:
        cli.main(["seed", "--profile", "prod"])
    assert info.value.code == 2


def test_seed_profiles_are_demo_and_isolation() -> None:
    assert cli.SEED_PROFILES == ("demo", "isolation")


def test_rules_seed_path_resolves() -> None:
    assert cli.RULES_SEED_PATH.is_file()
    assert cli.RULES_SEED_PATH.name == "rules.seed.json"


def test_cli_seed_has_no_assert_or_print() -> None:
    for module in (Path(cli.__file__), Path(cli.__file__).parent / "db/seed_ops.py"):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        asserts = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Assert)]
        prints = [
            n.lineno
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "print"
        ]
        assert (asserts, prints) == ([], []), module.name


def test_cli_holds_no_sql() -> None:
    source = Path(cli.__file__).read_text(encoding="utf-8")
    assert "text(" not in source
    assert "INSERT" not in source.upper().replace("INSERTED", "")


def test_seed_tenant_ids_deterministic() -> None:
    from app.db.seed_ops import seed_tenant_id

    code = "from app.db.seed_ops import seed_tenant_id as s; print(s('acme'))"
    other = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        cwd=Path(cli.__file__).parents[1],
    )
    assert other.stdout.strip() == str(seed_tenant_id("acme"))
    assert seed_tenant_id("acme") != seed_tenant_id("bharat")
