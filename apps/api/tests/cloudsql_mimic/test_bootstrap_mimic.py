"""0000 as a non-superuser CREATEROLE role that owns the database (Cloud SQL `postgres`):
DATABASE.md:1752, :1754 and open decision 3; CANON.md:75."""

from __future__ import annotations

import pytest

from .conftest import Mimic

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
