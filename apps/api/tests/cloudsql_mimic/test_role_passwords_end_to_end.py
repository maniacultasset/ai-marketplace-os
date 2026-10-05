"""S5a end to end on the Cloud SQL mimic, written by the tester: helper verifiers for any printable
ASCII password log in over TCP; each role then gets its 0000 session settings; a 0000 rerun keeps
the passwords and repairs a dropped extension (AC 1, 3, 5, 6)."""

from __future__ import annotations

import psycopg
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from .conftest import BOOTSTRAP_SQL, Mimic
from .test_bootstrap_mimic import (
    QUIET,
    ROLES,
    _passwords,
    _reset_passwords,
    _run_passwords,
    _verifier,
)

pytestmark = [pytest.mark.cloudsql_mimic, pytest.mark.allow_hosts(["127.0.0.1"])]

PRINTABLE_ASCII = st.characters(min_codepoint=0x20, max_codepoint=0x7E)


def _connect(mimic: Mimic, role: str, password: str) -> psycopg.Connection[tuple[object, ...]]:
    conninfo = f"host=127.0.0.1 port={mimic.host_port} dbname=mimic user={role} connect_timeout=10"
    return psycopg.connect(conninfo, password=password, autocommit=True)


@settings(
    max_examples=12,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(password=st.text(alphabet=PRINTABLE_ASCII, min_size=1, max_size=64))
def test_role_passwords_any_printable_ascii_password_logs_in(mimic: Mimic, password: str) -> None:
    _reset_passwords(mimic, boot_settings=QUIET)
    try:
        verifier = _verifier(password)
        run = _run_passwords(mimic, {"APP_RO_SCRAM": verifier})
        assert run.returncode == 0, run.stderr
        assert _passwords(mimic) == {"app_rw": "", "app_ro": verifier, "migrator": ""}
        with _connect(mimic, "app_ro", password) as conn:
            assert conn.execute("SELECT current_user").fetchone() == ("app_ro",)
    finally:
        _reset_passwords(mimic)


def test_role_passwords_all_three_set_and_roles_get_their_settings(mimic: Mimic) -> None:
    _reset_passwords(mimic, boot_settings=QUIET)
    plain = {role: f"pw-tester-{role}-s5a" for role in ROLES}
    env = {f"{role.upper()}_SCRAM": _verifier(plain[role]) for role in ROLES}
    try:
        run = _run_passwords(mimic, env)
        assert run.returncode == 0, run.stderr
        assert _passwords(mimic) == {role: env[f"{role.upper()}_SCRAM"] for role in ROLES}
        with _connect(mimic, "migrator", plain["migrator"]) as conn:
            assert conn.execute("SHOW lock_timeout").fetchone() == ("5s",)
            assert conn.execute("SHOW timezone").fetchone() == ("UTC",)
        with _connect(mimic, "app_rw", plain["app_rw"]) as conn:
            assert conn.execute("SHOW idle_in_transaction_session_timeout").fetchone() == ("1min",)
            assert conn.execute("SHOW transaction_read_only").fetchone() == ("off",)
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("SET ROLE migrator")
            conn.execute("SET ROLE app_platform_ro")
            conn.execute("RESET ROLE")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("CREATE TABLE public.s5_tester_probe (id int)")
        with _connect(mimic, "app_ro", plain["app_ro"]) as conn:
            assert conn.execute("SHOW transaction_read_only").fetchone() == ("on",)
            with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                conn.execute("CREATE TEMP TABLE s5_tester_probe (id int)")
        with pytest.raises(psycopg.OperationalError):
            _connect(mimic, "app_platform_ro", plain["app_ro"])
    finally:
        _reset_passwords(mimic)


def test_bootstrap_rerun_keeps_passwords_and_repairs_extension(mimic: Mimic) -> None:
    """0000 is additive (DATABASE.md:1815): a rerun as the CREATEROLE role never touches a role
    password, and restores a trusted extension dropped since."""
    _reset_passwords(mimic, boot_settings=QUIET)
    try:
        env = {"APP_RW_SCRAM": _verifier("pw-tester-rerun-s5a")}
        assert _run_passwords(mimic, env).returncode == 0
        before = _passwords(mimic)
        mimic.psql("DROP EXTENSION pg_trgm")
        rerun = mimic.run_sql_file(BOOTSTRAP_SQL.read_text(encoding="utf-8"))
        assert rerun.returncode == 0, rerun.stderr
        assert _passwords(mimic) == before
        assert mimic.psql("SELECT count(*) FROM pg_extension WHERE extname = 'pg_trgm'") == [("1",)]
    finally:
        _reset_passwords(mimic)
