"""0000 as a non-superuser CREATEROLE role that owns the database (Cloud SQL `postgres`):
DATABASE.md:1752, :1754 and open decision 3; CANON.md:75."""

from __future__ import annotations

import secrets
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager

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
# log_min_error_statement=panic keeps a failing ALTER ROLE (and its verifier) out of the server log;
# on Cloud SQL it is an instance flag, here boot's role setting (or the file sets it, see below).
PANIC = "log_min_error_statement = panic"
QUIET = ("log_statement = 'none'", PANIC)


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
@pytest.mark.parametrize(
    "bad",
    [
        "plaintext",
        "quote",
        "space",
        "truncated",
        "shaped",
        "zero-iterations",
        "bad-salt",
        "inner-pad-salt",
        "short-key",
        "unpadded-key",
    ],
)
def test_role_passwords_bad_value_refused_before_send(mimic: Mimic, bad: str, logging: str) -> None:
    """The shell check alone stops a bad value: under boot `none` the server check passes, and
    the other values look like verifiers. A value PostgreSQL cannot parse as a verifier would be
    hashed as a plaintext password, so the shell checks every field of the helper format."""
    _reset_passwords(mimic, boot_settings=QUIET if logging == "none" else ())
    shaped = _verifier("pw-s5-valid-0")
    value = {
        "plaintext": LEAK,
        "quote": f"{shaped}'{LEAK}",
        "space": f"{shaped} {LEAK}",
        "truncated": shaped[:-20],
        "shaped": "SCRAM-SHA-256$4096:x$y:z",
        "zero-iterations": shaped.replace("$4096:", "$0:", 1),
        "bad-salt": "SCRAM-SHA-256$4096:x$" + shaped.split("$", 2)[2],
        "inner-pad-salt": "SCRAM-SHA-256$4096:c2=sdA==$" + shaped.split("$", 2)[2],
        "short-key": shaped[:-20] + "=",
        "unpadded-key": shaped[:-1],
    }[bad]
    env = {"APP_RW_SCRAM": _verifier("pw-s5-valid-1"), "APP_RO_SCRAM": value}
    run = _run_passwords(mimic, env)
    assert run.returncode == 3, run.stderr
    assert "is not a SCRAM-SHA-256 verifier" in run.stderr
    assert _all_null(mimic)
    logs = mimic.logs()
    assert LEAK not in logs
    assert "SCRAM-SHA-256$" not in logs


def test_role_passwords_verifier_sets_and_logs_in(mimic: Mimic) -> None:
    _reset_passwords(mimic, boot_settings=QUIET)
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
        ("log_statement = 'all'", PANIC),
        ("log_statement = 'ddl'", PANIC),
        ("log_statement = 'mod'", PANIC),
        (*QUIET, "log_min_duration_statement = 0"),
        (*QUIET, "log_transaction_sample_rate = 1"),
        (*QUIET, "debug_print_parse = on"),
        (*QUIET, "debug_print_rewritten = on"),
        (*QUIET, "debug_print_plan = on"),
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


def _pgss_text(mimic: Mimic) -> str:
    return "\n".join(row[0] for row in mimic.psql("SELECT query FROM pg_stat_statements"))


def test_role_passwords_refused_while_pg_stat_statements_tracks_utility(pgss_mimic: Mimic) -> None:
    """pg_stat_statements keeps utility-statement text verbatim and on disk, so the file refuses
    until utility tracking is off for its session (the Cloud SQL flag
    pg_stat_statements.track_utility=off), then sets the password without storing the verifier."""
    _reset_passwords(pgss_mimic, boot_settings=(PANIC,))
    verifier = _verifier(secrets.token_urlsafe(18))
    server_key = verifier.rsplit(":", 1)[1]
    try:
        run = _run_passwords(pgss_mimic, {"APP_RW_SCRAM": verifier})
        assert run.returncode == 3, run.stderr
        assert "no password was set" in run.stderr
        assert _all_null(pgss_mimic)
        assert server_key not in _pgss_text(pgss_mimic)
        _reset_passwords(
            pgss_mimic, boot_settings=("pg_stat_statements.track_utility = off", PANIC)
        )
        run = _run_passwords(pgss_mimic, {"APP_RW_SCRAM": verifier})
        assert run.returncode == 0, run.stderr
        assert _passwords(pgss_mimic) == {"app_rw": verifier, "app_ro": "", "migrator": ""}
        assert server_key not in _pgss_text(pgss_mimic)
        assert "SCRAM-SHA-256$" not in pgss_mimic.logs()
    finally:
        _reset_passwords(pgss_mimic)


def test_role_passwords_refused_without_admin_on_roles(mimic: Mimic) -> None:
    _reset_passwords(mimic, boot_settings=QUIET)
    mimic.psql("CREATE ROLE boot2 LOGIN CREATEROLE")
    try:
        for setting in QUIET:
            mimic.psql(f"ALTER ROLE boot2 SET {setting}")
        run = _run_passwords(mimic, {"APP_RW_SCRAM": _verifier("pw-s5-valid-2")}, user="boot2")
        assert run.returncode == 3, run.stderr
        # Refused by pw_safe before any ALTER ROLE, not by the ALTER failing.
        assert "no password was set" in run.stderr
        assert _all_null(mimic)
        # A failing ALTER ROLE would put the verifier in the log (log_min_error_statement).
        assert "SCRAM-SHA-256$" not in mimic.logs()
    finally:
        mimic.psql("DROP ROLE boot2")


def test_role_passwords_refused_when_a_role_is_missing(mimic: Mimic) -> None:
    _reset_passwords(mimic, boot_settings=QUIET)
    mimic.psql("ALTER ROLE app_ro RENAME TO app_ro_s5_away")
    try:
        run = _run_passwords(mimic, {"APP_RW_SCRAM": _verifier("pw-s5-valid-3")})
        assert run.returncode == 3, run.stderr
        assert "no password was set" in run.stderr
        rows = mimic.psql("SELECT rolpassword IS NULL FROM pg_authid WHERE rolname = 'app_rw'")
        assert rows == [("t",)]
        assert "SCRAM-SHA-256$" not in mimic.logs()
    finally:
        mimic.psql("ALTER ROLE app_ro_s5_away RENAME TO app_ro")


# ---- an ALTER ROLE that fails after pw_safe (fix round 2: F5, F6) ----

HOLDER_APP = "s5_role_lock_holder"
_HOLDER_WAITING = (
    "SELECT count(*) FROM pg_stat_activity "
    "WHERE application_name = 's5_role_lock_holder' AND query LIKE '%pg_sleep%'"
)
_HOLDER_KILL = (
    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
    "WHERE application_name = 's5_role_lock_holder'"
)


@contextmanager
def _role_row_locked(mimic: Mimic, role: str) -> Iterator[None]:
    """Another session holds `role`'s pg_authid row (an open ALTER ROLE), so an ALTER ROLE of it
    waits; with lock_timeout the wait fails after the pw_safe check has passed."""
    holder = subprocess.Popen(
        [
            *("docker", "exec", "-i", mimic.name, "psql", "-h", "127.0.0.1"),
            *("-U", "postgres", "-d", "mimic", "-X", "-q"),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    assert holder.stdin is not None
    holder.stdin.write(
        f"SET application_name = '{HOLDER_APP}';\n"
        f"BEGIN; ALTER ROLE {role} CONNECTION LIMIT 7; SELECT pg_sleep(60); ROLLBACK;\n"
    )
    holder.stdin.close()
    try:
        deadline = time.monotonic() + 30
        while mimic.psql(_HOLDER_WAITING) != [("1",)]:
            assert time.monotonic() < deadline, "the lock holder never started"
            time.sleep(0.2)
        yield
    finally:
        mimic.psql(_HOLDER_KILL)
        holder.wait(timeout=30)


@contextmanager
def _boot_may_set_panic(mimic: Mimic, granted: bool) -> Iterator[None]:
    """With `granted`, boot may SET log_min_error_statement (as a superuser may), so the file sets
    panic itself; otherwise panic comes from boot's role setting, as from a Cloud SQL flag."""
    if granted:
        mimic.psql("GRANT SET ON PARAMETER log_min_error_statement TO boot")
    try:
        yield
    finally:
        if granted:
            mimic.psql("REVOKE SET ON PARAMETER log_min_error_statement FROM boot")


@pytest.mark.parametrize("panic_from", ["role-setting", "file-set"])
@pytest.mark.parametrize("cause", ["lock-timeout", "read-only"])
def test_role_passwords_failing_alter_never_logs_verifier(
    mimic: Mimic, cause: str, panic_from: str
) -> None:
    """pw_safe passes, then the ALTER ROLE fails. At the default log_min_error_statement=error
    the server would log the failing statement, verifier included."""
    granted = panic_from == "file-set"
    _reset_passwords(mimic, boot_settings=("log_statement = 'none'",) if granted else QUIET)
    verifier = _verifier(secrets.token_urlsafe(18))
    env = {"APP_RW_SCRAM": verifier}
    with _boot_may_set_panic(mimic, granted):
        if cause == "lock-timeout":
            with _role_row_locked(mimic, "app_rw"):
                run = _run_passwords(mimic, {**env, "PGOPTIONS": "-c lock_timeout=1s"})
            assert "lock timeout" in run.stderr
        else:
            run = _run_passwords(mimic, {**env, "PGOPTIONS": "-c default_transaction_read_only=on"})
            assert "read-only transaction" in run.stderr
    assert run.returncode == 3, run.stderr
    assert _all_null(mimic)
    logs = mimic.logs()
    assert "SCRAM-SHA-256$" not in logs
    assert verifier.rsplit(":", 1)[1] not in logs


def test_role_passwords_refused_while_failing_statements_are_logged(mimic: Mimic) -> None:
    """boot may not SET log_min_error_statement and it is not panic: refused before any ALTER."""
    _reset_passwords(mimic, boot_settings=("log_statement = 'none'",))
    run = _run_passwords(mimic, {"APP_RW_SCRAM": _verifier("pw-s5-valid-4")})
    assert run.returncode == 3, run.stderr
    assert "no password was set" in run.stderr
    assert _all_null(mimic)
    assert "SCRAM-SHA-256$" not in mimic.logs()


def test_role_passwords_all_or_nothing_when_a_later_alter_fails(mimic: Mimic) -> None:
    """app_rw's ALTER succeeds, app_ro's then times out: the transaction leaves both NULL."""
    _reset_passwords(mimic, boot_settings=QUIET)
    env = {
        "APP_RW_SCRAM": _verifier("pw-s5-valid-5"),
        "APP_RO_SCRAM": _verifier("pw-s5-valid-6"),
        "PGOPTIONS": "-c lock_timeout=1s",
    }
    with _role_row_locked(mimic, "app_ro"):
        run = _run_passwords(mimic, env)
    assert run.returncode == 3, run.stderr
    assert "lock timeout" in run.stderr
    assert _all_null(mimic)
    assert "SCRAM-SHA-256$" not in mimic.logs()
