"""Tester attacks on 0005_rls (S10b), raw SQL as the `app_rw` login: the SELECT-only named
branches never open a write or a row lock, an upsert onto another tenant's row by a global unique
key fails instead of rewriting it, and the side doors (COPY, pg_stats, the security-invoker views,
a cached plan, a temp-schema shadow of the helpers, a malformed GUC) never show tenant B's rows
under tenant A's GUC."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql

from .factories import World, build_world

Conn = psycopg.Connection[tuple[Any, ...]]


@pytest.fixture(scope="module")
def world(clean_module_admin_conn: Conn) -> World:
    """Two tenants, plus one user who is an active member of both (the switcher case)."""
    conn = clean_module_admin_conn
    built = build_world(conn, 2)
    a, b = built.tenants
    both = uuid4()
    conn.execute(
        "INSERT INTO users (id, email, display_name, hashed_password) VALUES (%s, %s, 'Both', 'x')",
        [both, f"both-{both}@x.test"],
    )
    for tenant in (a, b):
        conn.execute(
            "INSERT INTO tenant_memberships (tenant_id, user_id, role) VALUES (%s, %s, 'operator')",
            [tenant, both],
        )
    built.platform_rows["both_user"] = both
    return built


@contextmanager
def session(conn: Conn, tenant: UUID | None = None, **gucs: str) -> Iterator[Conn]:
    """One rolled-back app_rw transaction with `app.tenant_id` and any other `app.*` GUC set."""
    with conn.transaction(force_rollback=True):
        if tenant is not None:
            conn.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant)])
        for name, value in gucs.items():
            conn.execute("SELECT set_config(%s, %s, true)", [f"app.{name}", value])
        yield conn


def _member_of(admin: Conn, tenant: UUID) -> UUID:
    row = admin.execute(
        "SELECT user_id FROM tenant_memberships WHERE tenant_id = %s AND user_id NOT IN "
        "(SELECT user_id FROM tenant_memberships GROUP BY user_id HAVING count(*) > 1) LIMIT 1",
        [tenant],
    ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _refresh_of(admin: Conn, tenant: UUID) -> tuple[UUID, bytes]:
    row = admin.execute(
        "SELECT id, token_hash FROM refresh_tokens WHERE tenant_id = %s LIMIT 1", [tenant]
    ).fetchone()
    assert row is not None, "the world has no refresh token for this tenant"
    return UUID(str(row[0])), bytes(row[1])


# ---- SELECT-only branches never open a write -------------------------------------------------


def test_attack_memberships_own_cannot_update_other_tenant(
    admin_conn: Conn, app_conn: Conn, world: World
) -> None:
    """The cross-tenant own-memberships branch is SELECT only: the caller sees its B membership
    but cannot promote itself there from A's session."""
    a, b = world.tenants
    both = world.platform_rows["both_user"]
    with session(app_conn, a, user_id=str(both)):
        seen = app_conn.execute(
            "SELECT tenant_id FROM tenant_memberships WHERE user_id = %s", [both]
        ).fetchall()
        assert {r[0] for r in seen} == {a, b}
        promote = app_conn.execute(
            "UPDATE tenant_memberships SET role = 'owner' WHERE user_id = %s AND tenant_id = %s",
            [both, b],
        )
        assert promote.rowcount == 0
    row = admin_conn.execute(
        "SELECT role FROM tenant_memberships WHERE user_id = %s AND tenant_id = %s", [both, b]
    ).fetchone()
    assert row == ("operator",)


def test_attack_tenants_switcher_cannot_update(
    admin_conn: Conn, app_conn: Conn, world: World
) -> None:
    a, b = world.tenants
    both = world.platform_rows["both_user"]
    before = admin_conn.execute("SELECT name FROM tenants WHERE id = %s", [b]).fetchone()
    with session(app_conn, a, user_id=str(both)):
        seen = {r[0] for r in app_conn.execute("SELECT id FROM tenants").fetchall()}
        assert seen == {a, b}
        assert (
            app_conn.execute("UPDATE tenants SET name = 'pwned' WHERE id = %s", [b]).rowcount == 0
        )
    assert admin_conn.execute("SELECT name FROM tenants WHERE id = %s", [b]).fetchone() == before


def test_attack_teammates_and_login_branches_cannot_update_users(
    admin_conn: Conn, app_conn: Conn, world: World
) -> None:
    """A teammate (users_teammates) or the login-email row (users_login) is readable, never
    writable: only users_self opens UPDATE."""
    a, _ = world.tenants
    teammate = _member_of(admin_conn, a)
    email_row = admin_conn.execute("SELECT email FROM users WHERE id = %s", [teammate]).fetchone()
    assert email_row is not None
    email = str(email_row[0]).lower()
    with session(app_conn, a, login_email=email):
        assert app_conn.execute("SELECT 1 FROM users WHERE id = %s", [teammate]).fetchone()
        stolen = app_conn.execute(
            "UPDATE users SET hashed_password = 'attacker' WHERE id = %s", [teammate]
        )
        assert stolen.rowcount == 0
    row = admin_conn.execute("SELECT hashed_password FROM users WHERE id = %s", [teammate])
    assert row.fetchone() != ("attacker",)


def test_attack_hash_branch_gives_no_row_lock(
    admin_conn: Conn, app_conn: Conn, world: World
) -> None:
    """SELECT … FOR UPDATE needs the UPDATE policy too, so the pre-GUC token-hash branch reads B's
    row but cannot lock it (the refresh flow must set the tenant GUC before it locks, CODE-PLAN Q10).
    """
    a, b = world.tenants
    token_id, token_hash = _refresh_of(admin_conn, b)
    with session(app_conn, a, token_hash=token_hash.hex()):
        hashed = app_conn.execute(
            "SELECT id FROM refresh_tokens WHERE token_hash = %s", [token_hash]
        ).fetchall()
        locked = app_conn.execute(
            "SELECT id FROM refresh_tokens WHERE token_hash = %s FOR UPDATE", [token_hash]
        ).fetchall()
    assert hashed == [(token_id,)]
    assert locked == []


# ---- upserts onto another tenant's row by a global unique key ---------------------------------


def test_attack_upsert_refresh_token_of_other_tenant_refused(
    admin_conn: Conn, app_conn: Conn, world: World
) -> None:
    """INSERT … ON CONFLICT (token_hash) DO UPDATE with B's hash, from A's session and with the
    hash branch open, raises 42501 and leaves B's token unrevoked."""
    a, b = world.tenants
    token_id, token_hash = _refresh_of(admin_conn, b)
    user = _member_of(admin_conn, a)
    family = uuid4()
    with (
        session(app_conn, a, token_hash=token_hash.hex()),
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        app_conn.execute(
            "INSERT INTO refresh_tokens (id, tenant_id, user_id, family_id, token_hash, expires_at) "
            "VALUES (%s, %s, %s, %s, %s, now() + interval '1 day') "
            "ON CONFLICT (token_hash) DO UPDATE SET revoked_at = now(), revoked_reason = 'pwned'",
            [uuid4(), a, user, family, token_hash],
        )
    row = admin_conn.execute(
        "SELECT revoked_at, revoked_reason FROM refresh_tokens WHERE id = %s", [token_id]
    ).fetchone()
    assert row == (None, None)


def test_attack_signup_upsert_cannot_take_over_existing_user(
    admin_conn: Conn, app_conn: Conn, world: World
) -> None:
    """The signup branch (INSERT on the login email) plus ON CONFLICT DO UPDATE on an existing
    user of tenant B must not rewrite that user's password hash."""
    _, b = world.tenants
    victim = _member_of(admin_conn, b)
    row = admin_conn.execute(
        "SELECT email, hashed_password FROM users WHERE id = %s", [victim]
    ).fetchone()
    assert row is not None
    email, before = str(row[0]), row[1]
    with (
        session(app_conn, None, login_email=email.lower()),
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        app_conn.execute(
            "INSERT INTO users (id, email, display_name, hashed_password) VALUES (%s, %s, 'x', 'x') "
            "ON CONFLICT ((lower(email))) DO UPDATE SET hashed_password = 'attacker'",
            [uuid4(), email],
        )
    after = admin_conn.execute("SELECT hashed_password FROM users WHERE id = %s", [victim])
    assert after.fetchone() == (before,)


def test_attack_upsert_performance_of_other_tenant_listing_refused(
    admin_conn: Conn, app_conn: Conn, world: World
) -> None:
    """`performance` is unique on (listing_id, date) without tenant_id: an upsert from A naming
    B's listing and date must fail, not overwrite B's numbers."""
    a, b = world.tenants
    row = admin_conn.execute(
        "SELECT listing_id, date, marketplace, orders FROM performance WHERE tenant_id = %s LIMIT 1",
        [b],
    ).fetchone()
    assert row is not None, "the world has no performance row for tenant B"
    listing, day, marketplace, orders = row
    with session(app_conn, a), pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute(
            "INSERT INTO performance (tenant_id, listing_id, date, marketplace, orders) "
            "VALUES (%s, %s, %s, %s, 999999) "
            "ON CONFLICT (listing_id, date) DO UPDATE SET orders = 999999",
            [a, listing, day, marketplace],
        )
    after = admin_conn.execute(
        "SELECT orders FROM performance WHERE tenant_id = %s AND listing_id = %s AND date = %s",
        [b, listing, day],
    ).fetchone()
    assert after == (orders,)


# ---- side doors -------------------------------------------------------------------------------


def test_attack_copy_to_respects_rls(app_conn: Conn, world: World) -> None:
    a, b = world.tenants
    with (
        session(app_conn, a),
        app_conn.cursor().copy("COPY products (tenant_id) TO STDOUT") as copy,
    ):
        seen = {bytes(chunk).decode().strip() for chunk in copy}
    tenants = {line for chunk in seen for line in chunk.splitlines() if line}
    assert tenants == {str(a)}
    assert str(b) not in tenants


def test_attack_pg_stats_hides_rls_tables(admin_conn: Conn, app_conn: Conn, world: World) -> None:
    """After ANALYZE, pg_stats would carry B's most common values; it hides RLS tables from a
    role that RLS applies to."""
    assert world.tenants
    admin_conn.execute("ANALYZE products, users, refresh_tokens")
    with session(app_conn, world.tenants[0]):
        row = app_conn.execute(
            "SELECT count(*) FROM pg_stats WHERE schemaname = 'public' "
            "AND tablename IN ('products', 'users', 'refresh_tokens')"
        ).fetchone()
    assert row == (0,)


@pytest.mark.parametrize("view", ["listing_status_display", "product_status_display"])
def test_attack_display_views_cross_tenant(
    admin_conn: Conn, app_conn: Conn, world: World, view: str
) -> None:
    a, b = world.tenants
    name = sql.Identifier(view)
    total = admin_conn.execute(
        sql.SQL("SELECT count(*) FROM {} WHERE tenant_id = %s").format(name), [b]
    ).fetchone()
    assert total is not None and int(str(total[0])) >= 1, f"the world has no {view} row of B"
    with session(app_conn, a):
        seen = {r[0] for r in app_conn.execute(sql.SQL("SELECT tenant_id FROM {}").format(name))}
    assert seen == {a}


def test_attack_cached_plan_follows_the_guc(app_conn: Conn, world: World) -> None:
    """A prepared (server-side, generic-plan) statement re-reads the GUC each execution: the
    initplan never freezes tenant A's id into a plan reused under B."""
    a, b = world.tenants
    query = "SELECT DISTINCT tenant_id FROM products"
    app_conn.execute("SET plan_cache_mode = force_generic_plan")
    try:
        for tenant in (a, b, a, b, a, b):
            with session(app_conn, tenant):
                seen = {r[0] for r in app_conn.execute(query, prepare=True).fetchall()}
            assert seen == {tenant}
    finally:
        app_conn.execute("RESET plan_cache_mode")


def test_attack_temp_schema_cannot_shadow_helpers(app_conn: Conn, world: World) -> None:
    """app_rw may create temp objects; a pg_temp `app_tenant_id()` or `current_setting()` and a
    search_path that puts pg_temp first never redirect the policies to tenant B."""
    a, b = world.tenants
    with session(app_conn, a):
        app_conn.execute(
            f"CREATE FUNCTION pg_temp.app_tenant_id() RETURNS uuid LANGUAGE sql "
            f"AS $$ SELECT '{b}'::uuid $$"
        )
        app_conn.execute(
            f"CREATE FUNCTION pg_temp.current_setting(text, boolean) RETURNS text LANGUAGE sql "
            f"AS $$ SELECT '{b}'::text $$"
        )
        app_conn.execute("SET LOCAL search_path = pg_temp, public, pg_catalog")
        seen = {r[0] for r in app_conn.execute("SELECT DISTINCT tenant_id FROM products")}
    assert seen == {a}


@pytest.mark.parametrize("value", ["not-a-uuid", " ", "00000000-0000-0000-0000-00000000000"])
def test_attack_malformed_tenant_guc_fails_closed(app_conn: Conn, world: World, value: str) -> None:
    """A malformed tenant GUC raises (22P02) instead of widening to every row."""
    assert world.tenants
    with (
        pytest.raises(psycopg.errors.InvalidTextRepresentation),
        session(app_conn, None, tenant_id=value),
    ):
        app_conn.execute("SELECT count(*) FROM products").fetchone()


def test_attack_jr_by_id_with_wrong_guc_type_fails_closed(app_conn: Conn, world: World) -> None:
    assert world.tenants
    with (
        pytest.raises(psycopg.errors.InvalidTextRepresentation),
        session(app_conn, None, job_id="1 OR true"),
    ):
        app_conn.execute("SELECT count(*) FROM job_runs").fetchone()


def test_attack_token_hash_guc_of_other_value_hides_rows(
    admin_conn: Conn, app_conn: Conn, world: World
) -> None:
    """A token-hash GUC that matches no token reveals nothing, with or without a tenant."""
    assert world.tenants
    wrong = hashlib.sha256(b"no such token").hexdigest()
    with session(app_conn, None, token_hash=wrong):
        for table in ("refresh_tokens", "invitations", "oauth_states"):
            count = sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
            row = app_conn.execute(count).fetchone()
            assert row == (0,), table


# ---- guards stay fail-closed under RLS -------------------------------------------------------

ISSUE_WITH_RULE = (
    "INSERT INTO listing_issues (tenant_id, listing_id, marketplace, source, field_path, "
    "error_code, error_class, retry_class, severity, message, dedupe_key, rule_id) "
    "VALUES (%s, %s, 'amazon', 'rules_engine', 'content.title', 'BANNED_TERM', "
    "'content_error', 'ai_correct', 'error', 'm', %s, %s)"
)


def _issue_with_rule(conn: Conn, tenant: UUID, listing: UUID, rule: UUID) -> None:
    dedupe = f"rules_engine|content.title|BANNED_TERM|{rule}"
    conn.execute(ISSUE_WITH_RULE, [tenant, listing, dedupe, rule])


def test_attack_issue_cannot_link_other_tenants_rule(app_conn: Conn, world: World) -> None:
    """`rules_read` hides B's rules from A, so listing_issues_guard must fail closed: A's issue
    naming B's rule, or a rule id that does not exist, is GRD01 alike (the FK bypasses RLS, so
    a fail-open guard would link across tenants and leak whether B's rule id exists)."""
    a, b = world.tenants
    listing = world.rows["listings"][a]
    for rule in (world.rows["rules"][b], uuid4()):
        with pytest.raises(psycopg.Error) as info, session(app_conn, a):
            _issue_with_rule(app_conn, a, listing, rule)
        assert info.value.sqlstate == "GRD01", info.value
    for rule in (world.rows["rules"][a], world.platform_rows["rules"]):
        with session(app_conn, a):
            _issue_with_rule(app_conn, a, listing, rule)
