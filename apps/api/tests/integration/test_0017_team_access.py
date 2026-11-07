"""Revision 0017_team_access (SI5, ADR-0029): the round trip, `password_reset_links` under FORCE
RLS with its hash branch and grants, the recovery definers `activate_tenant` and
`extend_tenant_trial` (AC12: GRD01 for a non-staff, non-admin session), the Google invitee definer,
the `tenants_platform_admin` policy and the allow-list, purge-order and erasure redefinitions."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from . import factories as f
from .conftest import ScratchDb, expect_sqlstate
from .integrity_support import as_app_rw, as_migrator, scalar, set_gucs

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]
MakeConfig = Callable[[Path | None], Config]

PREVIOUS = "0015_image_public_copies"
REVISION = "0017_team_access"
REDEFINED = (
    "public.audit_allowlist(text)",
    "public.purge_tenant_order()",
    "public.redact_user(uuid)",
)
DEFINERS = (
    "activate_tenant(uuid,text,text)",
    "extend_tenant_trial(uuid,integer,text,text)",
    "app_create_google_invitee(text,text,text)",
)


def _hash(n: int) -> bytes:
    return bytes([n % 256]) * 32


@contextmanager
def as_app_rw_login(conn: Conn) -> Iterator[Conn]:
    """`app_rw` as the *session* user, as the app logs in: the recovery definers treat a
    superuser session user as staff (CP-D3), so `SET ROLE` alone would test the staff path."""
    conn.execute("SET SESSION AUTHORIZATION app_rw")
    try:
        yield conn
    finally:
        conn.execute("RESET SESSION AUTHORIZATION")


def _fingerprint(db: ScratchDb) -> dict[str, Any]:
    with psycopg.connect(db.libpq, autocommit=True) as conn:
        bodies = {
            fn: scalar(conn, "SELECT md5(pg_get_functiondef(%s::regprocedure))", (fn,))
            for fn in REDEFINED
        }
        table = scalar(conn, "SELECT to_regclass('public.password_reset_links') IS NOT NULL")
        functions = scalar(
            conn,
            "SELECT count(*) FROM pg_proc WHERE proname = ANY(%s)",
            ([d.split("(")[0] for d in DEFINERS],),
        )
        policy = scalar(
            conn,
            "SELECT count(*) FROM pg_policies WHERE policyname = 'tenants_platform_admin'",
        )
    return {"bodies": bodies, "table": table, "functions": functions, "policy": policy}


# ---- round trip ----


def test_round_trip_up_down_up(alembic_env: MakeConfig, scratch_db: ScratchDb) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, PREVIOUS)
    before = _fingerprint(scratch_db)
    assert before["table"] is False and before["functions"] == 0 and before["policy"] == 0
    command.upgrade(cfg, REVISION)
    at_head = _fingerprint(scratch_db)
    assert at_head["table"] is True and at_head["functions"] == 3 and at_head["policy"] == 1
    for fn in REDEFINED:
        assert at_head["bodies"][fn] != before["bodies"][fn], fn
    command.upgrade(cfg, "head")
    command.check(cfg)
    command.downgrade(cfg, PREVIOUS)
    assert _fingerprint(scratch_db) == before
    command.upgrade(cfg, REVISION)
    assert _fingerprint(scratch_db) == at_head
    command.upgrade(cfg, "head")
    command.check(cfg)


# ---- password_reset_links ----


def _link(conn: Conn, tenant: UUID, user: UUID, issuer: UUID, n: int, **columns: Any) -> UUID:
    values: dict[str, Any] = {
        "tenant_id": tenant,
        "user_id": user,
        "issued_by_user_id": issuer,
        "token_hash": _hash(n),
        "expires_at": columns.pop("expires_at", None),
    }
    if values["expires_at"] is None:
        values["expires_at"] = scalar(conn, "SELECT now() + interval '24 hours'")
    return f.insert(conn, "password_reset_links", **{**values, **columns})  # type: ignore[no-any-return]


def _team(conn: Conn) -> tuple[UUID, UUID, UUID]:
    tenant = f.tenant(conn)
    owner = f.member(conn, tenant, "owner")
    reviewer = f.member(conn, tenant, "reviewer")
    return tenant, owner, reviewer


def test_catalog_reset_links(db: Conn) -> None:
    row = db.execute(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
        "WHERE oid = 'public.password_reset_links'::regclass"
    ).fetchone()
    assert row == (True, True)
    policies = {
        r[0]: (r[1], r[2])
        for r in db.execute(
            "SELECT policyname, cmd, roles::text[] FROM pg_policies "
            "WHERE tablename = 'password_reset_links'"
        ).fetchall()
    }
    assert set(policies) == {"t_tenant", "t_platform", "t_migrator", "password_reset_links_hash"}
    assert policies["password_reset_links_hash"] == ("SELECT", ["app_rw"])
    checks = {
        r[0]
        for r in db.execute(
            "SELECT conname FROM pg_constraint WHERE contype = 'c' "
            "AND conrelid = 'public.password_reset_links'::regclass"
        ).fetchall()
    }
    assert checks == {
        "ck_password_reset_links_token_hash",
        "ck_password_reset_links_used_revoked",
        "ck_password_reset_links_ttl",
    }


def test_reset_links_grants(db: Conn) -> None:
    row = db.execute(
        "SELECT has_table_privilege('app_rw', 'public.password_reset_links', 'TRUNCATE'), "
        "has_table_privilege('app_rw', 'public.password_reset_links', 'UPDATE'), "
        "has_table_privilege('app_rw', 'public.password_reset_links', 'DELETE'), "
        "has_table_privilege('app_rw', 'public.password_reset_links', 'INSERT'), "
        "has_column_privilege('app_rw', 'public.password_reset_links', 'used_at', 'UPDATE'), "
        "has_column_privilege('app_rw', 'public.password_reset_links', 'revoked_at', 'UPDATE'), "
        "has_column_privilege('app_rw', 'public.password_reset_links', 'expires_at', 'UPDATE'), "
        "has_column_privilege('app_rw', 'public.password_reset_links', 'token_hash', 'UPDATE')"
    ).fetchone()
    assert row == (False, False, True, True, True, True, False, False)
    for role in ("app_ro", "app_platform_ro"):
        db.execute(f"SET ROLE {role}")
        try:
            expect_sqlstate(db, "42501", "SELECT token_hash FROM password_reset_links")
            db.execute("SELECT id, user_id, expires_at FROM password_reset_links")
        finally:
            db.execute("RESET ROLE")


def test_reset_links_tenant_isolation_and_hash_branch(db: Conn) -> None:
    a, a_owner, a_reviewer = _team(db)
    b, b_owner, b_reviewer = _team(db)
    _link(db, a, a_reviewer, a_owner, 101)
    _link(db, b, b_reviewer, b_owner, 102)
    with as_app_rw(db, tenant=b):
        assert scalar(db, "SELECT count(*) FROM password_reset_links") == 1
        assert scalar(db, "SELECT tenant_id FROM password_reset_links") == b
        # a DELETE from tenant B's session removes none of A's rows
        db.execute("DELETE FROM password_reset_links WHERE user_id = %s", (a_reviewer,))
    assert scalar(db, "SELECT count(*) FROM password_reset_links WHERE tenant_id = %s", (a,)) == 1
    set_gucs(db, tenant_id=None, token_hash=_hash(101).hex())
    with as_app_rw(db):
        rows = db.execute("SELECT tenant_id, user_id FROM password_reset_links").fetchall()
        assert rows == [(a, a_reviewer)]
        # the hash branch is SELECT-only
        db.execute("UPDATE password_reset_links SET used_at = now()")
    assert scalar(db, "SELECT count(*) FROM password_reset_links WHERE used_at IS NOT NULL") == 0
    set_gucs(db, token_hash=None)


def test_reset_links_app_rw_cannot_truncate_or_move_columns(db: Conn) -> None:
    a, owner, reviewer = _team(db)
    _link(db, a, reviewer, owner, 110)
    with as_app_rw(db, tenant=a):
        expect_sqlstate(db, "42501", "TRUNCATE password_reset_links")
        expect_sqlstate(db, "42501", "UPDATE password_reset_links SET expires_at = now()")
        db.execute("UPDATE password_reset_links SET revoked_at = now()")
        assert scalar(db, "SELECT count(*) FROM password_reset_links WHERE revoked_at IS NULL") == 0


def test_reset_links_checks_and_keys(db: Conn) -> None:
    a, owner, reviewer = _team(db)
    stranger = f.user(db)
    expect_sqlstate(
        db,
        "23514",
        "INSERT INTO password_reset_links (tenant_id, user_id, issued_by_user_id, token_hash, "
        "expires_at) VALUES (%s, %s, %s, %s, now() + interval '1 hour')",
        (a, reviewer, owner, b"short"),
        constraint="ck_password_reset_links_token_hash",
    )
    expect_sqlstate(
        db,
        "23514",
        "INSERT INTO password_reset_links (tenant_id, user_id, issued_by_user_id, token_hash, "
        "expires_at) VALUES (%s, %s, %s, %s, now() + interval '25 hours')",
        (a, reviewer, owner, _hash(120)),
        constraint="ck_password_reset_links_ttl",
    )
    expect_sqlstate(
        db,
        "23514",
        "INSERT INTO password_reset_links (tenant_id, user_id, issued_by_user_id, token_hash, "
        "expires_at, used_at, revoked_at) VALUES (%s, %s, %s, %s, now(), now(), now())",
        (a, reviewer, owner, _hash(121)),
        constraint="ck_password_reset_links_used_revoked",
    )
    expect_sqlstate(
        db,
        "23503",
        "INSERT INTO password_reset_links (tenant_id, user_id, issued_by_user_id, token_hash, "
        "expires_at) VALUES (%s, %s, %s, %s, now())",
        (a, stranger, owner, _hash(122)),
        constraint="fk_password_reset_links_user_id",
    )
    expect_sqlstate(
        db,
        "23503",
        "INSERT INTO password_reset_links (tenant_id, user_id, issued_by_user_id, token_hash, "
        "expires_at) VALUES (%s, %s, %s, %s, now())",
        (a, reviewer, stranger, _hash(123)),
        constraint="fk_password_reset_links_issued_by_user_id",
    )
    _link(db, a, reviewer, owner, 124)
    expect_sqlstate(
        db,
        "23505",
        "INSERT INTO password_reset_links (tenant_id, user_id, issued_by_user_id, token_hash, "
        "expires_at) VALUES (%s, %s, %s, %s, now())",
        (a, reviewer, owner, _hash(125)),
        constraint="uq_password_reset_links_live",
    )
    db.execute("UPDATE password_reset_links SET used_at = now() WHERE user_id = %s", (reviewer,))
    _link(db, a, reviewer, owner, 126)  # a closed link frees the member


def test_reset_links_cascade_with_the_tenant(db: Conn) -> None:
    a, owner, reviewer = _team(db)
    _link(db, a, reviewer, owner, 130)
    with as_migrator(db):
        db.execute("SELECT purge_tenant(%s, 'data', true)", (a,))
        assert (
            scalar(db, "SELECT count(*) FROM password_reset_links WHERE tenant_id = %s", (a,)) == 0
        )
        _link(db, a, reviewer, owner, 131)
        db.execute("SELECT purge_tenant(%s, 'final', true)", (a,))
    assert scalar(db, "SELECT count(*) FROM password_reset_links WHERE tenant_id = %s", (a,)) == 0


# ---- activate_tenant ----


def _admin(conn: Conn) -> tuple[UUID, str]:
    staff_tenant = f.tenant(conn)
    email = f"admin-{uuid4().hex[:8]}@kuinn.test"
    admin = f.user(conn, email=email, is_platform_admin=True)
    f.insert(conn, "tenant_memberships", tenant_id=staff_tenant, user_id=admin, role="owner")
    return admin, email


def _status(conn: Conn, tenant: UUID) -> str:
    return str(scalar(conn, "SELECT status FROM tenants WHERE id = %s", (tenant,)))


@pytest.mark.parametrize(
    ("start", "outcome", "end"),
    [
        ("suspended", "activated", "active"),
        ("trial", "activated", "active"),
        ("active", "unchanged", "active"),
        ("offboarded", "refused", "offboarded"),
    ],
)
def test_activate_as_staff(db: Conn, start: str, outcome: str, end: str) -> None:
    extra: dict[str, Any] = {}
    if start == "trial":
        extra["trial_ends_at"] = "2099-01-01T00:00:00Z"
    if start == "offboarded":
        extra["offboarded_at"] = "2026-01-01T00:00:00Z"
    tenant = f.tenant(db, status=start, **extra)
    with as_migrator(db):
        got = scalar(db, "SELECT activate_tenant(%s, 'paid', 'ops@kuinn.test')", (tenant,))
    assert got == outcome
    assert _status(db, tenant) == end
    audits = scalar(
        db,
        "SELECT count(*) FROM audit_log WHERE tenant_id = %s AND action = 'tenant.active'",
        (tenant,),
    )
    assert audits == (1 if outcome == "activated" else 0)


def test_activate_unknown_tenant(db: Conn) -> None:
    with as_migrator(db):
        assert scalar(db, "SELECT activate_tenant(%s, 'paid', 'ops@kuinn.test')", (uuid4(),)) == (
            "not_found"
        )


def test_activate_as_platform_admin_of_another_tenant(db: Conn) -> None:
    admin, email = _admin(db)
    tenant = f.tenant(db, status="suspended")
    set_gucs(db, user_id=admin)
    with as_app_rw_login(db):
        assert scalar(
            db, "SELECT activate_tenant(%s, 'paid by bank transfer', NULL)", (tenant,)
        ) == ("activated")
    set_gucs(db, user_id=None)
    assert _status(db, tenant) == "active"
    row = db.execute(
        "SELECT actor_type, actor_user_id, actor_email, reason FROM audit_log "
        "WHERE tenant_id = %s AND action = 'tenant.active'",
        (tenant,),
    ).fetchall()
    assert row == [("staff", None, email, "paid by bank transfer")]


def test_ac12_non_admin_app_rw_gets_grd01(db: Conn) -> None:
    own = f.tenant(db, status="suspended")
    other = f.tenant(db, status="suspended")
    user = f.user(db)
    f.insert(db, "tenant_memberships", tenant_id=own, user_id=user, role="owner")
    for target in (own, other):
        set_gucs(db, user_id=user, tenant_id=own)
        with as_app_rw_login(db):
            expect_sqlstate(
                db, "GRD01", "SELECT activate_tenant(%s, 'r', 'x@kuinn.test')", (target,)
            )
            expect_sqlstate(
                db,
                "GRD01",
                "SELECT extend_tenant_trial(%s, 7, 'r', 'x@kuinn.test')",
                (target,),
            )
            expect_sqlstate(
                db, "GRD01", "SELECT set_tenant_lifecycle(%s, 'active', 'r')", (target,)
            )
    set_gucs(db, user_id=None, tenant_id=None)
    assert _status(db, own) == "suspended" and _status(db, other) == "suspended"


def test_activate_needs_a_reason_and_a_staff_email(db: Conn) -> None:
    tenant = f.tenant(db, status="suspended")
    with as_migrator(db):
        expect_sqlstate(db, "GRD01", "SELECT activate_tenant(%s, 'paid', NULL)", (tenant,))
        expect_sqlstate(db, "GRD01", "SELECT activate_tenant(%s, '', 'ops@kuinn.test')", (tenant,))
        expect_sqlstate(
            db, "GRD01", "SELECT activate_tenant(%s, %s, 'ops@kuinn.test')", (tenant, "x" * 501)
        )


# ---- extend_tenant_trial ----


def _expired_trial(conn: Conn) -> UUID:
    tenant = f.tenant(conn, status="trial", trial_ends_at="2026-01-01T00:00:00Z")
    with as_migrator(conn):
        conn.execute("SELECT expire_trials()")
    assert _status(conn, tenant) == "suspended"
    return tenant


def test_extend_a_trial_from_its_later_end(db: Conn) -> None:
    later = f.tenant(db, status="trial", trial_ends_at="2099-01-01T00:00:00Z")
    past = f.tenant(db, status="trial", trial_ends_at="2020-01-01T00:00:00Z")
    with as_migrator(db):
        assert scalar(
            db, "SELECT extend_tenant_trial(%s, 10, 'pilot', 'ops@kuinn.test')", (later,)
        ) == ("extended")
        assert scalar(
            db, "SELECT extend_tenant_trial(%s, 10, 'pilot', 'ops@kuinn.test')", (past,)
        ) == ("extended")
    assert scalar(
        db,
        "SELECT trial_ends_at = timestamptz '2099-01-11T00:00:00Z' FROM tenants WHERE id = %s",
        (later,),
    )
    assert scalar(
        db,
        "SELECT abs(extract(epoch FROM trial_ends_at - (now() + interval '10 days'))) < 60 "
        "FROM tenants WHERE id = %s",
        (past,),
    )
    reasons = db.execute(
        "SELECT actor_type, actor_email, reason FROM audit_log "
        "WHERE tenant_id = %s AND action = 'tenant.trial_extended'",
        (later,),
    ).fetchall()
    assert reasons == [("staff", "ops@kuinn.test", "pilot")]


def test_extend_returns_a_trial_expired_tenant_to_trial(db: Conn) -> None:
    tenant = _expired_trial(db)
    with as_migrator(db):
        assert scalar(
            db, "SELECT extend_tenant_trial(%s, 30, 'more time', 'ops@kuinn.test')", (tenant,)
        ) == ("extended")
        # the hourly expiry leaves it alone until its new end
        db.execute("SELECT expire_trials()")
    assert _status(db, tenant) == "trial"
    assert scalar(
        db,
        "SELECT abs(extract(epoch FROM trial_ends_at - (now() + interval '30 days'))) < 60 "
        "FROM tenants WHERE id = %s",
        (tenant,),
    )


def test_extend_refuses_staff_suspension_active_and_offboarded(db: Conn) -> None:
    by_staff = f.tenant(db, status="active")
    with as_migrator(db):
        db.execute("SELECT set_tenant_lifecycle(%s, 'suspended', 'unpaid')", (by_staff,))
    active = f.tenant(db, status="active")
    gone = f.tenant(db, status="offboarded", offboarded_at="2026-01-01T00:00:00Z")
    with as_migrator(db):
        for tenant in (by_staff, active, gone):
            assert (
                scalar(db, "SELECT extend_tenant_trial(%s, 7, 'r', 'ops@kuinn.test')", (tenant,))
                == "refused"
            )
        assert (
            scalar(db, "SELECT extend_tenant_trial(%s, 7, 'r', 'ops@kuinn.test')", (uuid4(),))
            == "not_found"
        )
    assert _status(db, by_staff) == "suspended"


@pytest.mark.parametrize("days", [0, 91, None])
def test_extend_days_bounds(db: Conn, days: int | None) -> None:
    tenant = f.tenant(db, status="trial", trial_ends_at="2099-01-01T00:00:00Z")
    with as_migrator(db):
        expect_sqlstate(
            db, "GRD01", "SELECT extend_tenant_trial(%s, %s, 'r', 'ops@kuinn.test')", (tenant, days)
        )


def test_extend_as_platform_admin(db: Conn) -> None:
    admin, email = _admin(db)
    tenant = _expired_trial(db)
    set_gucs(db, user_id=admin)
    with as_app_rw_login(db):
        assert scalar(db, "SELECT extend_tenant_trial(%s, 14, 'pilot', NULL)", (tenant,)) == (
            "extended"
        )
    set_gucs(db, user_id=None)
    assert (
        scalar(
            db,
            "SELECT actor_email FROM audit_log WHERE tenant_id = %s AND action = 'tenant.trial_extended'",
            (tenant,),
        )
        == email
    )


def test_recovery_definers_execute_for_app_rw_only(db: Conn) -> None:
    for fn in DEFINERS:
        row = db.execute(
            "SELECT p.prosecdef, pg_get_userbyid(p.proowner), array_to_string(p.proconfig, ','), "
            "has_function_privilege('app_rw', p.oid, 'EXECUTE'), "
            "has_function_privilege('app_ro', p.oid, 'EXECUTE'), "
            "has_function_privilege('app_platform_ro', p.oid, 'EXECUTE') "
            "FROM pg_proc p WHERE p.oid = %s::regprocedure",
            (f"public.{fn}",),
        ).fetchone()
        assert row is not None
        assert row[:2] == (True, "migrator"), fn
        assert "search_path=pg_catalog, public, pg_temp" in row[2], fn
        assert row[3:] == (True, False, False), fn


# ---- tenants_platform_admin ----


def test_platform_admin_reads_every_tenant(db: Conn) -> None:
    admin, _ = _admin(db)
    mine = f.tenant(db)
    other = f.tenant(db)
    user = f.user(db)
    f.insert(db, "tenant_memberships", tenant_id=mine, user_id=user, role="owner")
    set_gucs(db, user_id=admin)
    with as_app_rw(db):
        seen = {r[0] for r in db.execute("SELECT id FROM tenants").fetchall()}
    assert {mine, other} <= seen
    set_gucs(db, user_id=user, tenant_id=mine)
    with as_app_rw(db):
        seen = {r[0] for r in db.execute("SELECT id FROM tenants").fetchall()}
    assert seen == {mine}
    set_gucs(db, user_id=None, tenant_id=None)


# ---- app_create_google_invitee ----


def _invitation(conn: Conn, tenant: UUID, email: str, n: int, **columns: Any) -> UUID:
    defaults: dict[str, Any] = {
        "tenant_id": tenant,
        "email": email,
        "role": "reviewer",
        "token_hash": _hash(n),
        "expires_at": "2099-01-01T00:00:00Z",
    }
    return f.insert(conn, "invitations", **{**defaults, **columns})  # type: ignore[no-any-return]


def _invitee(conn: Conn, email: str, sub: str, n: int, *, as_email: str | None = None) -> Any:
    set_gucs(conn, login_email=as_email or email, token_hash=_hash(n).hex())
    with as_app_rw(conn):
        return scalar(conn, "SELECT app_create_google_invitee(%s, %s, 'Asha')", (email, sub))


def test_google_invitee_creates_a_password_less_user(db: Conn) -> None:
    tenant = f.tenant(db)
    _invitation(db, tenant, "asha@gmail.com", 140)
    user_id = _invitee(db, "asha@gmail.com", "sub-asha-0017", 140)
    assert user_id is not None
    row = db.execute(
        "SELECT hashed_password, auth_provider, google_sub, email_verified_at IS NOT NULL "
        "FROM users WHERE id = %s",
        (user_id,),
    ).fetchone()
    assert row == (None, "google", "sub-asha-0017", True)
    assert scalar(db, "SELECT count(*) FROM signup_requests WHERE user_id = %s", (user_id,)) == 0


def test_google_invitee_taken_email_or_sub_is_null(db: Conn) -> None:
    tenant = f.tenant(db)
    f.user(db, email="taken@gmail.com")
    f.user(db, email="other@gmail.com", google_sub="sub-taken-0017")
    _invitation(db, tenant, "taken@gmail.com", 141)
    _invitation(db, tenant, "fresh@gmail.com", 142)
    assert _invitee(db, "taken@gmail.com", "sub-new-0017", 141) is None
    assert _invitee(db, "fresh@gmail.com", "sub-taken-0017", 142) is None


def test_google_invitee_guards(db: Conn) -> None:
    tenant = f.tenant(db)
    _invitation(db, tenant, "asha2@gmail.com", 143)
    _invitation(db, tenant, "late@gmail.com", 144, expires_at="2020-01-01T00:00:00Z")
    # another email than the login GUC
    set_gucs(db, login_email="asha2@gmail.com", token_hash=_hash(143).hex())
    with as_app_rw(db):
        expect_sqlstate(
            db, "GRD01", "SELECT app_create_google_invitee('other@gmail.com', 's', 'n')"
        )
    # no invitation for the hash, or one for another email, or an expired one
    for email, n in (("asha2@gmail.com", 199), ("late@gmail.com", 143), ("late@gmail.com", 144)):
        set_gucs(db, login_email=email, token_hash=_hash(n).hex())
        with as_app_rw(db):
            expect_sqlstate(
                db, "GRD01", "SELECT app_create_google_invitee(%s, 's-g', 'n')", (email,)
            )
    set_gucs(db, login_email=None, token_hash=None)


# ---- allow-list, purge order, erasure ----


def test_invitation_audit_keeps_only_the_role(db: Conn) -> None:
    assert scalar(db, "SELECT audit_allowlist('invitation')") == ["role"]
    tenant = f.tenant(db)
    invite = uuid4()
    db.execute(
        "SELECT audit(%s, 'system', NULL, 'invitation.created', 'invitation', %s, NULL, "
        '\'{"role": "admin", "email": "x@y.co"}\'::jsonb, NULL, NULL, NULL, NULL, NULL)',
        (tenant, invite),
    )
    assert scalar(db, "SELECT after FROM audit_log WHERE entity_id = %s", (invite,)) == {
        "role": "admin"
    }


def test_purge_order_holds_reset_links_once_before_invitations(db: Conn) -> None:
    order = scalar(db, "SELECT purge_tenant_order()")
    assert order.count("password_reset_links") == 1
    assert order.index("password_reset_links") < order.index("invitations")


def test_redact_user_deletes_the_users_reset_links(db: Conn) -> None:
    a, owner, reviewer = _team(db)
    _link(db, a, reviewer, owner, 150)
    other = f.member(db, a, "operator")
    _link(db, a, other, owner, 151)
    with as_migrator(db):
        db.execute("SELECT redact_user(%s)", (reviewer,))
    assert (
        scalar(db, "SELECT count(*) FROM password_reset_links WHERE user_id = %s", (reviewer,)) == 0
    )
    assert scalar(db, "SELECT count(*) FROM password_reset_links WHERE user_id = %s", (other,)) == 1


def test_link_ttl_window_is_24_hours(db: Conn) -> None:
    a, owner, reviewer = _team(db)
    _link(
        db,
        a,
        reviewer,
        owner,
        160,
        expires_at=scalar(db, "SELECT now() + %s", (timedelta(hours=24),)),
    )
