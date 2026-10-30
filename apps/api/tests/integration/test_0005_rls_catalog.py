"""Revision 0005_rls in the catalog (DATABASE.md §4 "0005 template", §8.2 ledger row 0005, §8.2
"Proposed catalog assertions"): RLS is ENABLEd and FORCEd on every table with a `tenant_id` column
plus `users` and `tenants`, each with its exact policy set; only the platform tables,
`rate_limit_counters` and the procrastinate queue go without; every helper call in a policy is an
initplan `(SELECT app_…())`; and no policy recurses (42P17) for any role and GUC set."""

from __future__ import annotations

import re
from typing import Any

import psycopg
import pytest
from psycopg import sql

from .integrity_support import set_gucs

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]

TEMPLATE = frozenset({"t_tenant", "t_platform", "t_migrator"})
# The named branches beside (or instead of) the template, per table (DATABASE.md §5.1-§5.4).
POLICIES: dict[str, frozenset[str]] = {
    "tenants": TEMPLATE | {"tenants_switcher"},
    "users": frozenset(
        {"users_self", "users_login", "users_signup", "users_teammates", "users_platform_admin"}
    )
    | {"t_platform", "t_migrator"},
    "tenant_memberships": TEMPLATE | {"memberships_own"},
    "refresh_tokens": TEMPLATE | {"refresh_tokens_hash", "refresh_tokens_own"},
    "invitations": TEMPLATE | {"invitations_hash"},
    # 0006_oauth_pending replaced 0005's SELECT-only branch with a SELECT and a DELETE branch
    "oauth_states": TEMPLATE | {"oauth_states_hash_select", "oauth_states_hash_delete"},
    "job_runs": TEMPLATE | {"jr_by_id"},
    "rules": frozenset({"rules_read", "rules_write", "rules_toggle", "t_platform", "t_migrator"}),
    "audit_log": frozenset({"audit_read", "audit_insert", "t_platform", "t_migrator"}),
    # 0011 (DPP-1): the per-user MFA tables have no tenant_id; own rows through app.user_id
    "user_mfa_factors": frozenset({"umf_self", "t_platform", "t_migrator"}),
    "user_recovery_codes": frozenset({"urc_self", "t_platform", "t_migrator"}),
    "mfa_challenges": frozenset({"mc_self", "mc_hash", "t_platform", "t_migrator"}),
    "user_password_history": frozenset({"uph_self", "t_platform", "t_migrator"}),
    # 0012 (ADR-0020): Google sign-in flows have no tenant_id; one row through app.token_hash
    "login_flows": frozenset(
        {"lf_hash_select", "lf_hash_delete", "lf_insert", "t_platform", "t_migrator"}
    ),
}
MFA_TABLES = frozenset(
    {"user_mfa_factors", "user_recovery_codes", "mfa_challenges", "user_password_history"}
)
# Platform-global (DATABASE.md:160), the limiter (no tenant_id, DATABASE.md:1023) and the queue
# (it holds only job_run_id, DATABASE.md:952): the exact set without RLS.
NO_RLS = frozenset(
    {
        "category_templates",
        "marketplace_error_map",
        "attribute_definitions",
        "attribute_columns",
        "category_attributes",
        "listing_status_transitions",
        "rate_limit_counters",
        "alembic_version",
    }
)
HELPER_CALL = re.compile(r"app_[a-z_]+\(\)")
SUBSELECT_CALL = re.compile(r"SELECT app_[a-z_]+\(\)")


def _tables(conn: Conn) -> dict[str, tuple[bool, bool]]:
    rows = conn.execute(
        "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity FROM pg_class c "
        "WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'p')"
    ).fetchall()
    return {str(r[0]): (bool(r[1]), bool(r[2])) for r in rows}


def _tenant_tables(conn: Conn) -> set[str]:
    rows = conn.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_attribute a ON a.attrelid = c.oid "
        "WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'p') "
        "AND a.attname = 'tenant_id' AND NOT a.attisdropped"
    ).fetchall()
    return {str(r[0]) for r in rows}


def _policies(conn: Conn) -> dict[str, dict[str, tuple[str, frozenset[str]]]]:
    rows = conn.execute(
        "SELECT tablename, policyname, cmd, roles::text[] FROM pg_policies WHERE schemaname = 'public'"
    ).fetchall()
    found: dict[str, dict[str, tuple[str, frozenset[str]]]] = {}
    for table, name, cmd, roles in rows:
        found.setdefault(str(table), {})[str(name)] = (str(cmd), frozenset(roles))
    return found


def test_rls_forced_everywhere(db: Conn) -> None:
    tables = _tables(db)
    expected = _tenant_tables(db) | {"users", "tenants", "login_flows"} | MFA_TABLES
    assert len(expected) > 30, "is the database at head?"
    policies = _policies(db)
    for table in sorted(expected):
        assert tables[table] == (True, True), f"{table}: RLS not enabled and forced"
        assert set(policies.get(table, {})) == POLICIES.get(table, TEMPLATE), table


def test_rls_template_policies_shape(db: Conn) -> None:
    """t_tenant is every command for app_rw; t_platform is SELECT for the two read-only roles;
    t_migrator is every command for migrator (the owner, which FORCE does not exempt)."""
    want = {
        "t_tenant": ("ALL", frozenset({"app_rw"})),
        "t_platform": ("SELECT", frozenset({"app_platform_ro", "app_ro"})),
        "t_migrator": ("ALL", frozenset({"migrator"})),
    }
    for table, named in _policies(db).items():
        for name, shape in named.items():
            if name in want:
                assert shape == want[name], (table, name, shape)
            else:
                assert "migrator" not in shape[1] and "PUBLIC" not in shape[1], (table, name)


def test_rls_tables_allowlisted(db: Conn) -> None:
    without = {t for t, (enabled, _) in _tables(db).items() if not enabled}
    queue = {t for t in without if t.startswith("procrastinate_")}
    assert queue, "the procrastinate tables are missing"
    assert without - queue == NO_RLS


def test_rls_policies_use_subselect(db: Conn) -> None:
    rows = db.execute(
        "SELECT tablename, policyname, coalesce(qual, ''), coalesce(with_check, '') "
        "FROM pg_policies WHERE schemaname = 'public'"
    ).fetchall()
    assert rows
    for table, name, qual, check in rows:
        for expression in (str(qual), str(check)):
            calls = HELPER_CALL.findall(expression)
            wrapped = SUBSELECT_CALL.findall(expression)
            assert len(calls) == len(wrapped), (table, name, expression)


GUCS = {
    "tenant_id": "00000000-0000-0000-0000-00000000000a",
    "user_id": "00000000-0000-0000-0000-00000000000b",
    "login_email": "someone@x.test",
    "job_id": "00000000-0000-0000-0000-00000000000c",
    "token_hash": "00" * 32,
    "challenge_hash": "00" * 32,  # 0011 (DPP-1)
}


@pytest.mark.parametrize("role", ["app_rw", "app_platform_ro", "app_ro", "migrator"])
def test_rls_no_policy_recursion(db: Conn, role: str) -> None:
    """Every policy branch evaluates for every role with every GUC set: never 42P17 (users and
    tenants read tenant_memberships, whose policies read GUCs only)."""
    tables = sorted(t for t, (enabled, _) in _tables(db).items() if enabled)
    set_gucs(db, **GUCS)
    db.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
    for table in tables:
        try:
            with db.transaction():
                db.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table)))
                db.execute(
                    sql.SQL("SELECT 1 FROM {} WHERE false FOR UPDATE").format(sql.Identifier(table))
                )
        except psycopg.errors.InsufficientPrivilege:
            continue  # a role without a grant on the table; the privilege layer answers first
        except psycopg.errors.InvalidRecursion as error:  # pragma: no cover - the failure
            raise AssertionError(f"{role} on {table}: {error}") from error
