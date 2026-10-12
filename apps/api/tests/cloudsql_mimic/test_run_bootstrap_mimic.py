"""scripts/db/run_bootstrap.py (`scripts/deploy/gcp/deploy.sh db-init`) on the Cloud SQL mimic: as
the non-superuser `boot`, it reruns 0000 and sets the three role passwords from files, keeping no
verifier in the server log; a server that logs statements refuses before any password is sent."""

from __future__ import annotations

import secrets
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from .conftest import REPO_ROOT, Mimic
from .test_bootstrap_mimic import PANIC, QUIET, ROLES, _all_null, _passwords, _reset_passwords

pytestmark = [pytest.mark.cloudsql_mimic, pytest.mark.allow_hosts(["127.0.0.1"])]

RUNNER = REPO_ROOT / "scripts/db/run_bootstrap.py"


@pytest.fixture
def boot_login(mimic: Mimic) -> Iterator[str]:
    """`boot` logs in over TCP like the Cloud SQL `postgres` user (a throwaway password)."""
    token = secrets.token_hex(16)
    mimic.psql(f"ALTER ROLE boot PASSWORD '{token}'")
    try:
        yield token
    finally:
        mimic.psql("ALTER ROLE boot PASSWORD NULL")


def _run(
    mimic: Mimic, tmp_path: Path, login: str, passwords: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    (tmp_path / "login").write_text(login, encoding="ascii")
    args = [
        *(sys.executable, str(RUNNER), "--host", "127.0.0.1", "--port", str(mimic.host_port)),
        *("--dbname", "mimic", "--user", "boot", "--password-file", str(tmp_path / "login")),
    ]
    for role, password in passwords.items():
        (tmp_path / role).write_text(password + "\n", encoding="ascii")
        args += [f"--{role.replace('_', '-')}-password-file", str(tmp_path / role)]
    return subprocess.run(args, capture_output=True, text=True, timeout=120, check=False)


def test_runner_sets_role_passwords_as_boot_and_reruns(
    mimic: Mimic, boot_login: str, tmp_path: Path
) -> None:
    _reset_passwords(mimic, boot_settings=QUIET)
    plain = {role: secrets.token_hex(24) for role in ROLES}
    try:
        for _ in range(2):  # db-init is safe to rerun
            run = _run(mimic, tmp_path, boot_login, plain)
            assert run.returncode == 0, run.stderr
            assert run.stdout.splitlines() == [
                "run_bootstrap: 0000_bootstrap.sql OK",
                "run_bootstrap: role passwords set for app_rw, app_ro, migrator",
            ]
            for role in ROLES:
                conninfo = (
                    f"host=127.0.0.1 port={mimic.host_port} dbname=mimic user={role} "
                    "connect_timeout=10"
                )
                with psycopg.connect(conninfo, password=plain[role]) as conn:
                    assert conn.execute("SELECT current_user").fetchone() == (role,)
        verifiers = _passwords(mimic)
        assert all(v.startswith("SCRAM-SHA-256$4096:") for v in verifiers.values())
        logs = mimic.logs()
        for value in (*plain.values(), *verifiers.values()):
            assert value not in logs
            assert value not in run.stdout + run.stderr
        assert "SCRAM-SHA-256$" not in logs
    finally:
        _reset_passwords(mimic)


def test_runner_refuses_on_a_logging_server(mimic: Mimic, boot_login: str, tmp_path: Path) -> None:
    _reset_passwords(mimic, boot_settings=("log_statement = 'all'", PANIC))
    password = secrets.token_hex(24)
    try:
        run = _run(mimic, tmp_path, boot_login, {"app_rw": password})
        assert run.returncode == 1, run.stderr
        assert "no password was set" in run.stderr
        assert _all_null(mimic)
        logs = mimic.logs()
        assert password not in logs
        assert "SCRAM-SHA-256$" not in logs
    finally:
        _reset_passwords(mimic)


def test_runner_bootstrap_only_without_role_files(
    mimic: Mimic, boot_login: str, tmp_path: Path
) -> None:
    _reset_passwords(mimic, boot_settings=QUIET)
    run = _run(mimic, tmp_path, boot_login, {})
    assert run.returncode == 0, run.stderr
    assert "run_bootstrap: no role password given" in run.stdout
    assert _all_null(mimic)


def test_runner_bad_login_names_no_value(mimic: Mimic, tmp_path: Path) -> None:
    wrong = secrets.token_hex(16)
    run = _run(mimic, tmp_path, wrong, {})
    assert run.returncode == 1
    assert run.stderr.startswith("run_bootstrap: OperationalError")
    assert wrong not in run.stderr
