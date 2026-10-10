"""S26 T19 (AC 8; CODE-PLAN C4): the restore drill scripts (scripts/drill/restore.sh,
check_restore.py) and `make drill-restore`. The live drill is `pytest -m drill` and the target."""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

RESTORE = "scripts/drill/restore.sh"
CHECK = "scripts/drill/check_restore.py"


def _module(repo_root: Path) -> ModuleType:
    if "check_restore" in sys.modules:
        return sys.modules["check_restore"]
    spec = importlib.util.spec_from_file_location("check_restore", repo_root / CHECK)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_restore"] = module
    spec.loader.exec_module(module)
    return module


def test_drill_checks_listed(repo_root: Path) -> None:
    """Every AC 8 check is named, documented and run, in this order."""
    module = _module(repo_root)
    assert module.CHECKS == (
        "alembic_head",
        "table_counts",
        "procrastinate_objects",
        "forced_rls",
        "check_helpers_search_path_empty",
        "app_rw_tcp_login",
        "app_rw_no_guc_zero_rows",
        "tenant_guc_scoped",
        "token_enc_decrypts",
        "elapsed",
    )
    text = (repo_root / CHECK).read_text(encoding="utf-8")
    doc = module.__doc__ or ""
    for name in module.CHECKS:
        assert f"`{name}`" in doc, name
        assert f'"{name}"' in text, name
    assert module.MAX_SECONDS == 900
    assert "unseal(" in text and "from app.adapters.credentials import" in text
    assert "SET search_path = ''" in text and "force_rollback=True" in text
    assert "relforcerowsecurity" in text and "'app.tenant_id'" in text


def test_drill_elapsed_over_900_fails(repo_root: Path) -> None:
    module = _module(repo_root)
    assert module.check_elapsed(1000.0, 900, lambda: 1900.0).ok
    late = module.check_elapsed(1000.0, 900, lambda: 1901.0)
    assert not late.ok and "901 s" in late.detail


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://postgres@10.0.0.4:5432/x",
        "postgresql://postgres@db.example.com/x",
        "mysql://root@127.0.0.1/x",
    ],
)
def test_drill_check_refuses_non_local_dsn(repo_root: Path, tmp_path: Path, dsn: str) -> None:
    module = _module(repo_root)
    assert module.is_local_dsn("postgresql+psycopg://postgres@localhost:5702/mos_test")
    assert not module.is_local_dsn(dsn)
    secret = tmp_path / "pw"
    secret.write_text("x", encoding="utf-8")
    ok = "postgresql://postgres@127.0.0.1:1/x"
    for source, restored in ((dsn, ok), (ok, dsn)):
        args = ["--source-dsn", source, "--restored-dsn", restored]
        assert module.main([*args, "--app-rw-password-file", str(secret)]) == 2


def test_drill_restores_into_throwaway_container_after_bootstrap(repo_root: Path) -> None:
    text = (repo_root / RESTORE).read_text(encoding="utf-8")
    assert "set -euo pipefail" in text
    assert "createdb" not in text and "CREATE DATABASE" not in text  # never the source cluster
    dump = text.index("pg_dump -U postgres -Fc marketplace_os")
    run = text.index('docker run -d --rm --name "$container"')
    bootstrap = text.index("psql_in <scripts/db/0000_bootstrap.sql")
    passwords = text.index("<scripts/db/0000_role_passwords.sql")
    restore = text.index("pg_restore -U postgres -d marketplace_os --exit-on-error")
    check = text.index("uv run --frozen python scripts/drill/check_restore.py")
    assert dump < run < bootstrap < passwords < restore < check
    assert "-p 127.0.0.1::5432" in text  # loopback, a port Docker allocates
    assert 'docker rm -f "$container"' in text and "trap cleanup EXIT" in text
    assert "fresh_stack.sh down" in text and 'chmod 0700 "$work"' in text
    assert 'POSTGRES_PASSWORD="$PGPASSWORD" docker run' in text  # never on an argv
    assert '"$image"' in text and 'yaml.safe_load(open("docker-compose.yml"))' in text
    assert '--started "$started" --max-seconds "$MAX_SECONDS"' in text
    assert "mkos-fresh-*) ;;" in text  # only a fresh stack is a source
    assert "scripts/e2e/acceptance.sh" in text and "--seed-run" in text
    for url in re.findall(r"https?://[^\s\"'$*|)]+", text):
        assert url.startswith(("http://127.0.0.1", "http://localhost")), url


def test_drill_restore_rejects_unknown_argument(repo_root: Path) -> None:
    result = subprocess.run(
        [str(repo_root / RESTORE), "--bogus"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 2 and result.stderr.startswith("usage: restore.sh")


def test_api_tests_excludes_drill_marker(repo_root: Path) -> None:
    makefile = (repo_root / "Makefile").read_text(encoding="utf-8")
    markers = re.search(r"^API_TEST_MARKERS := (.+)$", makefile, re.MULTILINE)
    assert markers is not None and "not drill" in markers.group(1)
    assert '-m "$(API_TEST_MARKERS)"' in makefile
    tests = (repo_root / "apps/api/tests/drill/test_pg_dump_restore_round_trip.py").read_text(
        encoding="utf-8"
    )
    assert "pytest.mark.drill" in tests


def test_makefile_drill_restore_target(repo_root: Path) -> None:
    makefile = (repo_root / "Makefile").read_text(encoding="utf-8")
    assert (
        ".PHONY: drill-restore\ndrill-restore:\n\tscripts/drill/restore.sh --seed-run" in makefile
    )
    workflows = "\n".join(
        p.read_text(encoding="utf-8")
        for p in (repo_root / ".github/workflows").glob("*.yml")
        if p.name != "nightly.yml"
    )
    assert "drill-restore" not in workflows  # not a required check
