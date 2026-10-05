"""`db migrate` logged in as `migrator` over TCP, the Cloud Run job's path (CANON.md:75): no SET
ROLE, the lock held under `migrator`, 0000's default privileges on new tables, and no app-role
privilege on `alembic_version`. The password is a helper verifier set through the password file."""

from __future__ import annotations

import secrets
from pathlib import Path

import pytest
from tests.alembic_helpers import write_revision

from .conftest import BOOTSTRAP_SQL, Mimic
from .test_bootstrap_mimic import QUIET, _reset_passwords, _run_passwords, _verifier

pytestmark = [pytest.mark.cloudsql_mimic, pytest.mark.allow_hosts(["127.0.0.1"])]

DB = "mimic_mig"
APP_ROLES = ("app_rw", "app_ro", "app_platform_ro")
DML = ("SELECT", "INSERT", "UPDATE", "DELETE")
ASSERT_MIGRATOR_LOGIN_AND_LOCK = (
    "DO $$ BEGIN IF current_user <> 'migrator' OR session_user <> 'migrator' "
    "OR (SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted "
    "AND pid = pg_backend_pid()) <> 1 THEN RAISE EXCEPTION 'not migrator or no lock'; "
    "END IF; END $$"
)


def _privilege(mimic: Mimic, role: str, table: str, privilege: str) -> bool:
    rows = mimic.psql(f"SELECT has_table_privilege('{role}', '{table}', '{privilege}')", db=DB)
    return rows == [("t",)]


def test_migrate_as_migrator_applies_default_privileges(
    mimic: Mimic, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.cli import run_migrate
    from app.core.config import get_settings
    from app.db.migrations import alembic_config

    mimic.psql(f"CREATE DATABASE {DB} OWNER boot", db="postgres")
    try:
        mimic.psql("CREATE EXTENSION vector", db=DB)
        run = mimic.run_sql_file(BOOTSTRAP_SQL.read_text(encoding="utf-8"), db=DB, user="boot")
        assert run.returncode == 0, run.stderr
        _reset_passwords(mimic, boot_settings=QUIET)
        password = secrets.token_urlsafe(18)
        run = _run_passwords(mimic, {"MIGRATOR_SCRAM": _verifier(password)})
        assert run.returncode == 0, run.stderr

        versions = tmp_path / "versions"
        write_revision(
            versions,
            "s5m",
            [ASSERT_MIGRATOR_LOGIN_AND_LOCK, "CREATE TABLE probe (id bigint PRIMARY KEY)"],
            ["DROP TABLE probe"],
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("APP_ENV", "local")
        monkeypatch.setenv(
            "ALEMBIC_DATABASE_URL",
            f"postgresql+psycopg://migrator:{password}@127.0.0.1:{mimic.host_port}/{DB}",
        )
        monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://unused@127.0.0.1:1/unused")
        monkeypatch.setenv("JWT_SECRET", "ci_only_not_a_real_secret_at_least_32_chars")
        get_settings.cache_clear()
        cfg = alembic_config()
        cfg.set_main_option("version_locations", str(versions))
        try:
            assert run_migrate(cfg) == 0
        finally:
            get_settings.cache_clear()

        owners = mimic.psql(
            "SELECT tablename, tableowner FROM pg_tables WHERE schemaname = 'public' ORDER BY 1",
            db=DB,
        )
        assert owners == [("alembic_version", "migrator"), ("probe", "migrator")]
        for privilege in DML:
            assert _privilege(mimic, "app_rw", "probe", privilege), privilege
        for privilege in (*DML, "TRUNCATE"):
            for role in APP_ROLES:
                assert not _privilege(mimic, role, "alembic_version", privilege), (role, privilege)
        for privilege in DML:
            for role in ("app_ro", "app_platform_ro"):
                assert not _privilege(mimic, role, "probe", privilege), (role, privilege)
        public = mimic.psql(
            "SELECT count(*) FROM pg_class c, aclexplode(c.relacl) a "
            "WHERE c.oid = 'alembic_version'::regclass AND a.grantee = 0",
            db=DB,
        )
        assert public == [("0",)]
    finally:
        _reset_passwords(mimic)
        mimic.psql(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)", db="postgres")
