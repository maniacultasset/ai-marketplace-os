"""0000 as a non-superuser CREATEROLE role that owns the database (Cloud SQL `postgres`):
DATABASE.md:1752, :1754 and open decision 3; CANON.md:75."""

from __future__ import annotations

import secrets
import subprocess
import sys

import psycopg
import pytest

from .conftest import REPO_ROOT, Mimic, Run

pytestmark = [pytest.mark.cloudsql_mimic, pytest.mark.allow_hosts(["127.0.0.1"])]

PUBLIC_ACL = (
    "{migrator=UC/migrator,=U/migrator,app_rw=U/migrator,app_ro=U/migrator,"
    "app_platform_ro=U/migrator}"
)
DEFAULT_ACLS = [
    ("S", "public", "{app_rw=rU/migrator}"),
    ("f", "<global>", "{migrator=X/migrator}"),
    ("r", "public", "{app_rw=arwd/migrator}"),
]
_DEFAULT_ACL_QUERY = (
    "SELECT d.defaclobjtype, coalesce(n.nspname, '<global>'), d.defaclacl::text "
    "FROM pg_default_acl d LEFT JOIN pg_namespace n ON n.oid = d.defaclnamespace "
    "WHERE d.defaclrole = 'migrator'::regrole ORDER BY 1, 2"
)


def test_bootstrap_sql_mimic_runs_twice(mimic: Mimic) -> None:
    for db, runs in mimic.bootstrap_runs.items():
        assert len(runs) == 2, db
        for n, run in enumerate(runs, start=1):
            assert run.returncode == 0, f"{db} run {n} exited {run.returncode}:\n{run.stderr}"
    for n, run in enumerate(mimic.bootstrap_runs["mimic"], start=1):
        stray = [line for line in run.stderr.splitlines() if line and "NOTICE:" not in line]
        assert stray == [], f"mimic run {n} stderr: {run.stderr}"


def test_bootstrap_sql_mimic_owner_and_self_grant(mimic: Mimic) -> None:
    owner = mimic.psql("SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = 'public'")
    assert owner == [("migrator",)]
    assert mimic.psql("SELECT rolsuper FROM pg_roles WHERE rolname = 'boot'") == [("f",)]
    grant = mimic.psql(
        "SELECT inherit_option, set_option FROM pg_auth_members "
        "WHERE roleid = 'migrator'::regrole AND member = 'boot'::regrole "
        "AND grantor = 'boot'::regrole"
    )
    assert grant == [("f", "t")]


def test_bootstrap_sql_mimic_acls_match_superuser_path(mimic: Mimic) -> None:
    nspacl = "SELECT nspacl::text FROM pg_namespace WHERE nspname = 'public'"
    for db in ("mimic", "ref"):
        assert mimic.psql(nspacl, db=db) == [(PUBLIC_ACL,)], db
        assert mimic.psql(_DEFAULT_ACL_QUERY, db=db) == DEFAULT_ACLS, db


# ---- scripts/db/0000_role_passwords.sql (EXECUTION-PLAN.md:545; DATABASE.md:1752) ----

PASSWORD_SQL = REPO_ROOT / "scripts/db/0000_role_passwords.sql"
SCRAM_HELPER = REPO_ROOT / "scripts/db/scram_verifier.py"
ROLES = ("app_rw", "app_ro", "migrator")
LEAK = "leakpw_s5"


def _reset_passwords(mimic: Mimic, *, boot_settings: tuple[str, ...] = ()) -> None:
    """As the superuser: the three passwords NULL, and boot's role settings exactly as given."""
    for role in ROLES:
        mimic.psql(f"ALTER ROLE {role} PASSWORD NULL")
    mimic.psql("ALTER ROLE boot RESET ALL")
    for setting in boot_settings:
        mimic.psql(f"ALTER ROLE boot SET {setting}")


def _passwords(mimic: Mimic) -> dict[str, str]:
    rows = mimic.psql(
        "SELECT rolname, coalesce(rolpassword, '') FROM pg_authid "
        "WHERE rolname IN ('app_rw', 'app_ro', 'migrator')"
    )
    return {row[0]: row[1] for row in rows}


def _all_null(mimic: Mimic) -> bool:
    return _passwords(mimic) == dict.fromkeys(ROLES, "")


def _verifier(plaintext: str) -> str:
    result = subprocess.run(
        [sys.executable, str(SCRAM_HELPER)],
        input=plaintext,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return result.stdout.strip()


def _run_passwords(mimic: Mimic, env: dict[str, str], *, user: str = "boot") -> Run:
    return mimic.run_sql_file(
        PASSWORD_SQL.read_text(encoding="utf-8"), db="mimic", user=user, env=env
    )


def test_role_passwords_unset_or_empty_skip(mimic: Mimic) -> None:
    _reset_passwords(mimic)
    run = _run_passwords(mimic, {})
    assert run.returncode == 0, run.stderr
    assert _all_null(mimic)
    run = _run_passwords(mimic, {"APP_RW_SCRAM": ""})
    assert run.returncode == 0, run.stderr
    assert _all_null(mimic)


@pytest.mark.parametrize("logging", ["all", "none"])
@pytest.mark.parametrize("bad", ["plaintext", "quote", "space"])
def test_role_passwords_bad_value_refused_before_send(mimic: Mimic, bad: str, logging: str) -> None:
    """The shell check alone stops a bad value: under boot `none` the server check passes, and
    the quote and space values otherwise look like verifiers."""
    _reset_passwords(mimic, boot_settings=("log_statement = 'none'",) if logging == "none" else ())
    shaped = _verifier("pw-s5-valid-0")
    value = {"plaintext": LEAK, "quote": f"{shaped}'{LEAK}", "space": f"{shaped} {LEAK}"}[bad]
    env = {"APP_RW_SCRAM": _verifier("pw-s5-valid-1"), "APP_RO_SCRAM": value}
    run = _run_passwords(mimic, env)
    assert run.returncode == 3, run.stderr
    assert "is not a SCRAM-SHA-256 verifier" in run.stderr
    assert _all_null(mimic)
    logs = mimic.logs()
    assert LEAK not in logs
    assert "SCRAM-SHA-256$" not in logs


def test_role_passwords_verifier_sets_and_logs_in(mimic: Mimic) -> None:
    _reset_passwords(mimic, boot_settings=("log_statement = 'none'",))
    plaintext = secrets.token_urlsafe(18)
    verifier = _verifier(plaintext)
    run = _run_passwords(mimic, {"APP_RW_SCRAM": verifier})
    assert run.returncode == 0, run.stderr
    assert _passwords(mimic) == {"app_rw": verifier, "app_ro": "", "migrator": ""}
    conninfo = f"host=127.0.0.1 port={mimic.host_port} dbname=mimic user=app_rw connect_timeout=10"
    with psycopg.connect(conninfo, password=plaintext) as conn:
        assert conn.execute("SELECT current_user").fetchone() == ("app_rw",)
    with pytest.raises(psycopg.OperationalError) as caught:
        psycopg.connect(conninfo, password=plaintext + "x")
    assert "password authentication failed" in str(caught.value)
    logs = mimic.logs()
    assert plaintext not in logs
    assert verifier not in logs
    assert "SCRAM-SHA-256$" not in logs


@pytest.mark.parametrize(
    "boot_settings",
    [
        ("log_statement = 'all'",),
        ("log_statement = 'ddl'",),
        ("log_statement = 'mod'",),
        ("log_statement = 'none'", "log_min_duration_statement = 0"),
        ("log_statement = 'none'", "log_transaction_sample_rate = 1"),
        ("log_statement = 'none'", "debug_print_parse = on"),
        ("log_statement = 'none'", "debug_print_rewritten = on"),
        ("log_statement = 'none'", "debug_print_plan = on"),
    ],
    ids=[
        "all",
        "ddl",
        "mod",
        "duration-0",
        "transaction-sample",
        "debug-parse",
        "debug-rewritten",
        "debug-plan",
    ],
)
def test_role_passwords_refused_while_server_logs_statements(
    mimic: Mimic, boot_settings: tuple[str, ...]
) -> None:
    _reset_passwords(mimic, boot_settings=boot_settings)
    plaintext = secrets.token_urlsafe(18)
    run = _run_passwords(mimic, {"APP_RW_SCRAM": _verifier(plaintext)})
    assert run.returncode == 3, run.stderr
    assert "no password was set" in run.stderr
    assert _all_null(mimic)
    logs = mimic.logs()
    assert "SCRAM-SHA-256$" not in logs
    assert plaintext not in logs


def test_role_passwords_refused_without_admin_on_roles(mimic: Mimic) -> None:
    _reset_passwords(mimic, boot_settings=("log_statement = 'none'",))
    mimic.psql("CREATE ROLE boot2 LOGIN CREATEROLE")
    try:
        mimic.psql("ALTER ROLE boot2 SET log_statement = 'none'")
        run = _run_passwords(mimic, {"APP_RW_SCRAM": _verifier("pw-s5-valid-2")}, user="boot2")
        assert run.returncode == 3, run.stderr
        assert _all_null(mimic)
        # A failing ALTER ROLE would put the verifier in the log (log_min_error_statement).
        assert "SCRAM-SHA-256$" not in mimic.logs()
    finally:
        mimic.psql("DROP ROLE boot2")


def test_role_passwords_refused_when_a_role_is_missing(mimic: Mimic) -> None:
    _reset_passwords(mimic, boot_settings=("log_statement = 'none'",))
    mimic.psql("ALTER ROLE app_ro RENAME TO app_ro_s5_away")
    try:
        run = _run_passwords(mimic, {"APP_RW_SCRAM": _verifier("pw-s5-valid-3")})
        assert run.returncode == 3, run.stderr
        rows = mimic.psql("SELECT rolpassword IS NULL FROM pg_authid WHERE rolname = 'app_rw'")
        assert rows == [("t",)]
        assert "SCRAM-SHA-256$" not in mimic.logs()
    finally:
        mimic.psql("ALTER ROLE app_ro_s5_away RENAME TO app_ro")
