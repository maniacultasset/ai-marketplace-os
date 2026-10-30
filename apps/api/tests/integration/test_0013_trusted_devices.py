"""Revision 0013_trusted_devices (ADR-0023): the round trip, the catalog, `trusted_devices` under
FORCE RLS, the guard (the admin 7-day cap, token-only updates, `prev_token_hash` on rotation), the
grants (no hash for the read-only roles, no TRUNCATE), the CHECKs, the prune definer and the
erasure redefinitions. Run in the Docker batch (`make test-db`, `make api-tests`)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

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

PREVIOUS = "0012_google_signin"
REVISION = "0013_trusted_devices"
ERASURE_FUNCTIONS = ("public.redact_user(uuid)", "public.purge_tenant(uuid,text,boolean)")
NEW_COLUMNS = (
    ("mfa_challenges", "trust_device"),
    ("refresh_tokens", "device_id"),
    ("refresh_tokens", "device_auth_at"),
)


def _hash(n: int) -> bytes:
    return bytes([n % 256]) * 32


def _fingerprint(db: ScratchDb) -> dict[str, Any]:
    with psycopg.connect(db.libpq, autocommit=True) as conn:
        bodies = {
            fn: scalar(conn, "SELECT md5(pg_get_functiondef(%s::regprocedure))", (fn,))
            for fn in ERASURE_FUNCTIONS
        }
        table = scalar(conn, "SELECT to_regclass('public.trusted_devices') IS NOT NULL")
        columns = sorted(
            (table_name, column)
            for table_name, column in NEW_COLUMNS
            if scalar(
                conn,
                "SELECT count(*) FROM pg_attribute WHERE attrelid = %s::regclass "
                "AND attname = %s AND NOT attisdropped",
                (f"public.{table_name}", column),
            )
        )
        prune = scalar(
            conn, "SELECT count(*) FROM pg_proc WHERE proname = 'app_prune_trusted_devices'"
        )
    return {"bodies": bodies, "table": table, "columns": columns, "prune": prune}


# ---- round trip ----


def test_round_trip_up_down_up(alembic_env: MakeConfig, scratch_db: ScratchDb) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, PREVIOUS)
    before = _fingerprint(scratch_db)
    assert before["table"] is False and before["columns"] == [] and before["prune"] == 0
    command.upgrade(cfg, REVISION)
    at_head = _fingerprint(scratch_db)
    assert at_head["table"] is True and len(at_head["columns"]) == 3 and at_head["prune"] == 1
    assert at_head["bodies"] != before["bodies"]
    command.upgrade(cfg, "head")
    command.check(cfg)
    command.downgrade(cfg, PREVIOUS)
    # the downgrade puts back 0012's erasure bodies
    assert _fingerprint(scratch_db) == before
    command.upgrade(cfg, REVISION)
    assert _fingerprint(scratch_db) == at_head
    command.upgrade(cfg, "head")
    command.check(cfg)


def _offline_sql(direction: str, span: str) -> str:
    import io
    from contextlib import redirect_stdout

    from app.db.migrations import alembic_config

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        getattr(command, direction)(alembic_config(), span, sql=True)
    return buffer.getvalue()


def test_lock_timeout_and_checks_not_valid_then_validated(
    alembic_env: MakeConfig, db: Conn
) -> None:
    alembic_env(None)
    up = _offline_sql("upgrade", f"{PREVIOUS}:{REVISION}")
    down = _offline_sql("downgrade", f"{REVISION}:{PREVIOUS}")
    assert up.index("SET lock_timeout = '5s'") < up.index("CREATE TABLE")
    assert down.index("SET lock_timeout = '5s'") < down.index("DROP")
    for name, table in (
        ("ck_mfa_challenges_trust_device", "mfa_challenges"),
        ("ck_refresh_tokens_device", "refresh_tokens"),
    ):
        added = up.index(f"ADD CONSTRAINT {name}")
        assert "NOT VALID" in up[added : up.index(";", added)]
        assert up.index(f"ALTER TABLE public.{table} VALIDATE CONSTRAINT {name}") > added
        assert scalar(db, "SELECT convalidated FROM pg_constraint WHERE conname = %s", (name,))


# ---- catalog and grants ----


def test_catalog_trusted_devices(db: Conn) -> None:
    row = db.execute(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
        "WHERE oid = 'public.trusted_devices'::regclass"
    ).fetchone()
    assert row == (True, True)
    policies = {
        r[0]
        for r in db.execute(
            "SELECT policyname FROM pg_policies WHERE tablename = 'trusted_devices'"
        ).fetchall()
    }
    assert policies == {"td_self", "t_platform", "t_migrator"}
    checks = {
        r[0]
        for r in db.execute(
            "SELECT conname FROM pg_constraint WHERE contype = 'c' "
            "AND conrelid = 'public.trusted_devices'::regclass"
        ).fetchall()
    }
    assert checks == {
        "ck_trusted_devices_hashes",
        "ck_trusted_devices_method",
        "ck_trusted_devices_label",
        "ck_trusted_devices_expiry",
    }


def test_app_rw_updates_only_the_token_columns_and_never_truncates(db: Conn) -> None:
    granted = {
        r[0]
        for r in db.execute(
            "SELECT column_name FROM information_schema.column_privileges "
            "WHERE table_name = 'trusted_devices' AND grantee = 'app_rw' "
            "AND privilege_type = 'UPDATE'"
        ).fetchall()
    }
    assert granted == {"token_hash", "prev_token_hash", "last_used_at"}
    assert not scalar(
        db, "SELECT has_table_privilege('app_rw', 'public.trusted_devices', 'TRUNCATE')"
    )
    assert not scalar(
        db, "SELECT has_table_privilege('app_rw', 'public.trusted_devices', 'UPDATE')"
    )


@pytest.mark.parametrize("role", ["app_ro", "app_platform_ro"])
def test_read_only_roles_never_read_a_hash(db: Conn, role: str) -> None:
    for column in ("token_hash", "prev_token_hash"):
        assert not scalar(
            db,
            "SELECT has_column_privilege(%s, 'public.trusted_devices', %s, 'SELECT')",
            (role, column),
        ), (role, column)
    for column in ("id", "user_id", "label", "trusted_at", "expires_at", "last_used_at"):
        assert scalar(
            db,
            "SELECT has_column_privilege(%s, 'public.trusted_devices', %s, 'SELECT')",
            (role, column),
        ), (role, column)
    for table, column in NEW_COLUMNS:
        assert scalar(
            db,
            "SELECT has_column_privilege(%s, %s, %s, 'SELECT')",
            (role, f"public.{table}", column),
        ), (role, table, column)


def test_prune_is_a_migrator_definer_only_app_rw_runs(db: Conn) -> None:
    row = db.execute(
        "SELECT p.prosecdef, pg_get_userbyid(p.proowner), array_to_string(p.proconfig, ',') "
        "FROM pg_proc p WHERE p.oid = 'public.app_prune_trusted_devices()'::regprocedure"
    ).fetchone()
    assert row is not None and row[0] is True and row[1] == "migrator"
    assert "search_path=pg_catalog, public, pg_temp" in row[2]
    for role in ("app_ro", "app_platform_ro"):
        db.execute(f"SET ROLE {role}")
        try:
            expect_sqlstate(db, "42501", "SELECT app_prune_trusted_devices()")
        finally:
            db.execute("RESET ROLE")


# ---- RLS, the guard and the CHECKs (as app_rw) ----


def _insert(
    conn: Conn,
    user_id: UUID,
    n: int,
    *,
    days: int = 30,
    trusted: str = "now()",
    extra: str = "",
    extra_values: tuple[Any, ...] = (),
) -> None:
    columns = "user_id, token_hash, method, label, trusted_at, expires_at" + extra
    marks = f"%s, %s, 'otp', 'Chrome on macOS', {trusted}, {trusted} + make_interval(days => %s)"
    if extra:
        marks += ", " + ", ".join(["%s"] * len(extra_values))
    conn.execute(
        f"INSERT INTO trusted_devices ({columns}) VALUES ({marks})",  # noqa: S608 - constants
        (user_id, _hash(n), days, *extra_values),
    )


def test_td_self_reaches_only_the_own_rows(db: Conn) -> None:
    me, other = f.user(db), f.user(db)
    _insert(db, other, 1)
    set_gucs(db, user_id=me)
    with as_app_rw(db):
        _insert(db, me, 2)
        assert scalar(db, "SELECT count(*) FROM trusted_devices") == 1
        assert db.execute("DELETE FROM trusted_devices WHERE user_id = %s", (other,)).rowcount == 0
        expect_sqlstate(db, "42501", "INSERT INTO trusted_devices (user_id, token_hash, method, "
                        "label, trusted_at, expires_at) VALUES (%s, %s, 'otp', 'x', now(), "
                        "now() + interval '1 day')", (other, _hash(3)))  # fmt: skip
    assert scalar(db, "SELECT count(*) FROM trusted_devices WHERE user_id = %s", (other,)) == 1


def test_guard_caps_an_admin_at_seven_days(db: Conn) -> None:
    admin = f.user(db, is_platform_admin=True)
    set_gucs(db, user_id=admin)
    with as_app_rw(db):
        expect_sqlstate(
            db,
            "GRD01",
            "INSERT INTO trusted_devices (user_id, token_hash, method, label, trusted_at, "
            "expires_at) VALUES (%s, %s, 'otp', 'x', now(), now() + interval '30 days')",
            (admin, _hash(4)),
        )
        _insert(db, admin, 5, days=7)


def test_guard_new_rows_start_from_a_recent_unused_code(db: Conn) -> None:
    me = f.user(db)
    set_gucs(db, user_id=me)
    with as_app_rw(db):
        for sql, params in (
            (
                "INSERT INTO trusted_devices (user_id, token_hash, method, label, trusted_at, "
                "expires_at) VALUES (%s, %s, 'otp', 'x', now() - interval '20 minutes', "
                "now() + interval '1 day')",
                (me, _hash(6)),
            ),
            (
                "INSERT INTO trusted_devices (user_id, token_hash, method, label, trusted_at, "
                "expires_at, last_used_at) VALUES (%s, %s, 'otp', 'x', now(), "
                "now() + interval '1 day', now())",
                (me, _hash(7)),
            ),
            (
                "INSERT INTO trusted_devices (user_id, token_hash, prev_token_hash, method, "
                "label, trusted_at, expires_at) VALUES (%s, %s, %s, 'otp', 'x', now(), "
                "now() + interval '1 day')",
                (me, _hash(8), _hash(9)),
            ),
        ):
            expect_sqlstate(db, "GRD01", sql, params)


def test_guard_rotation_keeps_the_old_token(db: Conn) -> None:
    me = f.user(db)
    set_gucs(db, user_id=me)
    with as_app_rw(db):
        _insert(db, me, 10)
        expect_sqlstate(db, "GRD01", "UPDATE trusted_devices SET token_hash = %s", (_hash(11),))
        expect_sqlstate(
            db, "GRD01", "UPDATE trusted_devices SET prev_token_hash = %s", (_hash(12),)
        )
        db.execute(
            "UPDATE trusted_devices SET token_hash = %s, prev_token_hash = %s, "
            "last_used_at = now()",
            (_hash(13), _hash(10)),
        )
        expect_sqlstate(db, "42501", "UPDATE trusted_devices SET expires_at = now()")
    assert bytes(scalar(db, "SELECT prev_token_hash FROM trusted_devices")) == _hash(10)


@pytest.mark.parametrize(
    ("sql", "constraint"),
    [
        (
            "INSERT INTO trusted_devices (user_id, token_hash, method, label, trusted_at, "
            "expires_at) VALUES (%s, '\\x00', 'otp', 'x', now(), now() + interval '1 day')",
            "ck_trusted_devices_hashes",
        ),
        (
            "INSERT INTO trusted_devices (user_id, token_hash, method, label, trusted_at, "
            "expires_at) VALUES (%s, decode(repeat('ab', 32), 'hex'), 'rc', 'x', now(), "
            "now() + interval '1 day')",
            "ck_trusted_devices_method",
        ),
        (
            "INSERT INTO trusted_devices (user_id, token_hash, method, label, trusted_at, "
            "expires_at) VALUES (%s, decode(repeat('ab', 32), 'hex'), 'otp', '', now(), "
            "now() + interval '1 day')",
            "ck_trusted_devices_label",
        ),
        (
            "INSERT INTO trusted_devices (user_id, token_hash, method, label, trusted_at, "
            "expires_at) VALUES (%s, decode(repeat('ab', 32), 'hex'), 'otp', 'x', now(), "
            "now() + interval '31 days')",
            "ck_trusted_devices_expiry",
        ),
    ],
)
def test_checks(db: Conn, sql: str, constraint: str) -> None:
    me = f.user(db)
    expect_sqlstate(db, "23514", sql, (me,), constraint=constraint)


def test_challenge_trust_device_only_with_otp(db: Conn) -> None:
    me = f.user(db)
    expect_sqlstate(
        db,
        "23514",
        "INSERT INTO mfa_challenges (challenge_hash, user_id, purpose, amr, expires_at, "
        "trust_device) VALUES (%s, %s, 'login', ARRAY['pwd'], now() + interval '5 minutes', true)",
        (_hash(20), me),
        constraint="ck_mfa_challenges_trust_device",
    )


def test_refresh_device_id_needs_device_auth_at(db: Conn) -> None:
    tenant = f.tenant(db)
    me = f.member(db, tenant)
    expect_sqlstate(
        db,
        "23514",
        "INSERT INTO refresh_tokens (tenant_id, user_id, family_id, token_hash, expires_at, "
        "device_id) VALUES (%s, %s, gen_random_uuid(), %s, now() + interval '1 day', "
        "gen_random_uuid())",
        (tenant, me, _hash(21)),
        constraint="ck_refresh_tokens_device",
    )
    db.execute(
        "INSERT INTO refresh_tokens (tenant_id, user_id, family_id, token_hash, expires_at, "
        "device_auth_at) VALUES (%s, %s, gen_random_uuid(), %s, now() + interval '1 day', now())",
        (tenant, me, _hash(22)),
    )


def test_prune_deletes_only_expired_rows(db: Conn) -> None:
    me = f.user(db)
    _insert(db, me, 30)
    db.execute(
        "INSERT INTO trusted_devices (user_id, token_hash, method, label, trusted_at, expires_at) "
        "VALUES (%s, %s, 'otp', 'x', now() - interval '40 days', now() - interval '10 days')",
        (me, _hash(31)),
    )
    with as_app_rw(db):
        assert scalar(db, "SELECT app_prune_trusted_devices()") == 1
    assert scalar(db, "SELECT count(*) FROM trusted_devices") == 1


# ---- erasure ----


def test_redact_user_deletes_trusted_browsers(db: Conn) -> None:
    tenant = f.tenant(db)
    me = f.member(db, tenant)
    _insert(db, me, 40)
    with as_migrator(db):
        db.execute("SELECT redact_user(%s)", (me,))
    assert scalar(db, "SELECT count(*) FROM trusted_devices WHERE user_id = %s", (me,)) == 0


def test_purge_tenant_data_deletes_browsers_of_pseudonymised_users_only(db: Conn) -> None:
    tenant, other_tenant = f.tenant(db), f.tenant(db)
    only_here = f.member(db, tenant)
    also_elsewhere = f.member(db, tenant)
    f.insert(
        db, "tenant_memberships", tenant_id=other_tenant, user_id=also_elsewhere, role="operator"
    )
    _insert(db, only_here, 50)
    _insert(db, also_elsewhere, 51)
    with as_migrator(db):
        db.execute("SELECT purge_tenant(%s, 'data', true)", (tenant,))
    assert scalar(db, "SELECT count(*) FROM trusted_devices WHERE user_id = %s", (only_here,)) == 0
    assert (
        scalar(db, "SELECT count(*) FROM trusted_devices WHERE user_id = %s", (also_elsewhere,))
        == 1
    )
