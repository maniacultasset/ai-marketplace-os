"""Revision 0012_google_signin (ADR-0020): the round trip, the catalog, `login_flows` under FORCE
RLS, the credential and verified-via guards, the five definer functions, the widened challenge
CHECK, the read-only grants and the erasure redefinitions. Run in the Docker batch
(`make test-db`, `make api-tests`)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
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

PREVIOUS = "0011b_account_purge"
REVISION = "0012_google_signin"
ERASURE_FUNCTIONS = ("public.redact_user(uuid)", "public.purge_tenant(uuid,text,boolean)")
DEFINERS = (
    "app_google_user(text)",
    "app_create_google_applicant(text,text,text,text,text,text,text)",
    "app_link_google_sub(uuid,text)",
    "app_unlink_google_sub(uuid)",
    "app_prune_login_flows()",
)
NEW_COLUMNS = (
    ("users", "google_sub"),
    ("signup_requests", "verified_via"),
    ("mfa_challenges", "link_google_sub"),
)


def _hash(n: int) -> bytes:
    return bytes([n % 256]) * 32


def _fingerprint(db: ScratchDb) -> dict[str, Any]:
    with psycopg.connect(db.libpq, autocommit=True) as conn:
        bodies = {
            fn: scalar(conn, "SELECT md5(pg_get_functiondef(%s::regprocedure))", (fn,))
            for fn in ERASURE_FUNCTIONS
        }
        table = scalar(conn, "SELECT to_regclass('public.login_flows') IS NOT NULL")
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
        amr = scalar(
            conn,
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'ck_mfa_challenges_amr'",
        )
        functions = scalar(
            conn,
            "SELECT count(*) FROM pg_proc WHERE proname = ANY(%s)",
            ([d.split("(")[0] for d in DEFINERS],),
        )
    return {
        "bodies": bodies,
        "table": table,
        "columns": columns,
        "amr": amr,
        "functions": functions,
    }


# ---- round trip ----


def test_round_trip_up_down_up(alembic_env: MakeConfig, scratch_db: ScratchDb) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, PREVIOUS)
    before = _fingerprint(scratch_db)
    assert before["table"] is False and before["columns"] == [] and before["functions"] == 0
    assert "fed" not in before["amr"]
    command.upgrade(cfg, REVISION)
    at_head = _fingerprint(scratch_db)
    assert at_head["table"] is True and len(at_head["columns"]) == 3
    assert at_head["functions"] == len(DEFINERS) and "fed" in at_head["amr"]
    assert at_head["bodies"] != before["bodies"]
    command.upgrade(cfg, "head")
    command.check(cfg)
    command.downgrade(cfg, PREVIOUS)
    # the downgrade puts back 0011's erasure bodies and the narrower CHECK
    assert _fingerprint(scratch_db) == before
    command.upgrade(cfg, REVISION)
    assert _fingerprint(scratch_db) == at_head
    command.upgrade(cfg, "head")
    command.check(cfg)


def test_downgrade_drops_fed_challenges_first(
    alembic_env: MakeConfig, scratch_db: ScratchDb
) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, REVISION)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        user_id = f.user(conn)
        conn.execute(
            "INSERT INTO mfa_challenges (challenge_hash, user_id, purpose, amr, expires_at) "
            "VALUES (%s, %s, 'enroll', ARRAY['fed'], now() + interval '5 minutes')",
            (_hash(7), user_id),
        )
    command.downgrade(cfg, PREVIOUS)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert scalar(conn, "SELECT count(*) FROM mfa_challenges") == 0
    command.upgrade(cfg, "head")


def _offline_sql(direction: str, span: str) -> str:
    import io
    from contextlib import redirect_stdout

    from app.db.migrations import alembic_config

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        getattr(command, direction)(alembic_config(), span, sql=True)
    return buffer.getvalue()


def test_lock_timeout_5s_both_directions(alembic_env: MakeConfig) -> None:
    alembic_env(None)
    up = _offline_sql("upgrade", f"{PREVIOUS}:{REVISION}")
    down = _offline_sql("downgrade", f"{REVISION}:{PREVIOUS}")
    assert up.index("SET lock_timeout = '5s'") < up.index("ALTER TABLE")
    assert down.index("SET lock_timeout = '5s'") < down.index("DROP INDEX")


def test_checks_added_not_valid_then_validated(alembic_env: MakeConfig, db: Conn) -> None:
    alembic_env(None)
    up = _offline_sql("upgrade", f"{PREVIOUS}:{REVISION}")
    for name, table in (
        ("ck_users_google_sub", "users"),
        ("ck_signup_requests_verified_via", "signup_requests"),
        ("ck_mfa_challenges_link_google_sub", "mfa_challenges"),
        ("ck_mfa_challenges_amr", "mfa_challenges"),
    ):
        added = up.index(f"ADD CONSTRAINT {name}")
        assert "NOT VALID" in up[added : up.index(";", added)]
        assert up.index(f"ALTER TABLE public.{table} VALIDATE CONSTRAINT {name}") > added
        assert scalar(db, "SELECT convalidated FROM pg_constraint WHERE conname = %s", (name,))


def test_users_index_built_concurrently(alembic_env: MakeConfig, db: Conn) -> None:
    alembic_env(None)
    up = _offline_sql("upgrade", f"{PREVIOUS}:{REVISION}")
    assert "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_users_google_sub" in up
    assert scalar(
        db,
        "SELECT indisvalid AND indisunique FROM pg_index "
        "WHERE indexrelid = 'public.uq_users_google_sub'::regclass",
    )


# ---- catalog ----


def test_catalog_login_flows(db: Conn) -> None:
    row = db.execute(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
        "WHERE oid = 'public.login_flows'::regclass"
    ).fetchone()
    assert row == (True, True)
    policies = {
        r[0]
        for r in db.execute(
            "SELECT policyname FROM pg_policies WHERE tablename = 'login_flows'"
        ).fetchall()
    }
    assert policies == {"lf_hash_select", "lf_hash_delete", "lf_insert", "t_platform", "t_migrator"}
    checks = {
        r[0]
        for r in db.execute(
            "SELECT conname FROM pg_constraint WHERE contype = 'c' "
            "AND conrelid = 'public.login_flows'::regclass"
        ).fetchall()
    }
    assert checks == {
        "ck_login_flows_phase",
        "ck_login_flows_hashes",
        "ck_login_flows_phase_columns",
        "ck_login_flows_bounds",
    }


def test_definers_are_security_definer_owned_by_migrator(db: Conn) -> None:
    for fn in DEFINERS:
        row = db.execute(
            "SELECT p.prosecdef, pg_get_userbyid(p.proowner), "
            "array_to_string(p.proconfig, ',') FROM pg_proc p WHERE p.oid = %s::regprocedure",
            (f"public.{fn}",),
        ).fetchone()
        assert row is not None
        assert row[0] is True and row[1] == "migrator", fn
        assert "search_path=pg_catalog, public, pg_temp" in row[2], fn


def test_only_app_rw_executes_the_definers(db: Conn) -> None:
    for role in ("app_ro", "app_platform_ro"):
        db.execute(f"SET ROLE {role}")
        try:
            expect_sqlstate(db, "42501", "SELECT app_google_user('x')")
            expect_sqlstate(db, "42501", "SELECT app_prune_login_flows()")
            expect_sqlstate(db, "42501", "SELECT app_unlink_google_sub(%s)", (uuid4(),))
        finally:
            db.execute("RESET ROLE")


# ---- login_flows under FORCE RLS ----


def _insert_flow(
    conn: Conn, n: int, *, phase: str = "handle", ttl: timedelta = timedelta(seconds=60)
) -> None:
    identity = phase != "authorize"
    conn.execute(
        "INSERT INTO login_flows (phase, flow_hash, binding_hash, code_verifier, nonce_hash, sub, "
        "email, expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s, now() + %s)",
        (
            phase,
            _hash(n),
            _hash(n + 1),
            None if identity else "v" * 43,
            None if identity else _hash(n + 2),
            f"sub-{n}" if identity else None,
            f"g{n}@gmail.com" if identity else None,
            ttl,
        ),
    )


def test_app_rw_reads_and_deletes_only_the_named_flow(db: Conn) -> None:
    with as_app_rw(db):
        _insert_flow(db, 10)
        _insert_flow(db, 20)
        assert scalar(db, "SELECT count(*) FROM login_flows") == 0
        set_gucs(db, token_hash=_hash(10).hex())
        assert scalar(db, "SELECT count(*) FROM login_flows") == 1
        assert scalar(db, "SELECT sub FROM login_flows") == "sub-10"
        db.execute("DELETE FROM login_flows")
        set_gucs(db, token_hash=None)
    assert scalar(db, "SELECT count(*) FROM login_flows WHERE flow_hash = %s", (_hash(10),)) == 0
    assert scalar(db, "SELECT count(*) FROM login_flows WHERE flow_hash = %s", (_hash(20),)) == 1


def test_app_rw_cannot_update_or_truncate_flows(db: Conn) -> None:
    with as_app_rw(db):
        _insert_flow(db, 30)
        set_gucs(db, token_hash=_hash(30).hex())
        expect_sqlstate(db, "42501", "UPDATE login_flows SET sub = 'other'")
        expect_sqlstate(db, "42501", "TRUNCATE login_flows")


def test_app_rw_insert_ttl_capped_at_15_minutes(db: Conn) -> None:
    with as_app_rw(db):
        _insert_flow(db, 40, phase="details", ttl=timedelta(minutes=15))
        expect_sqlstate(
            db,
            "42501",
            "INSERT INTO login_flows (phase, flow_hash, binding_hash, sub, email, expires_at) "
            "VALUES ('details', %s, %s, 's', 'a@gmail.com', now() + interval '16 minutes')",
            (_hash(50), _hash(51)),
        )
        expect_sqlstate(
            db,
            "42501",
            "INSERT INTO login_flows (phase, flow_hash, binding_hash, sub, email, expires_at) "
            "VALUES ('details', %s, %s, 's', 'a@gmail.com', now() - interval '1 second')",
            (_hash(52), _hash(53)),
        )


@pytest.mark.parametrize(
    ("phase", "verifier", "nonce", "sub", "email"),
    [
        ("authorize", None, _hash(1), None, None),
        ("authorize", "v" * 43, None, None, None),
        ("authorize", "v" * 43, _hash(1), "s", "a@gmail.com"),
        ("handle", "v" * 43, None, "s", "a@gmail.com"),
        ("handle", None, None, None, None),
        ("handle", None, None, "s", None),
        ("bogus", None, None, "s", "a@gmail.com"),
    ],
)
def test_flow_phase_columns_checked(
    db: Conn, phase: str, verifier: str | None, nonce: bytes | None, sub: str | None,
    email: str | None,
) -> None:  # fmt: skip
    expect_sqlstate(
        db,
        "23514",
        "INSERT INTO login_flows (phase, flow_hash, binding_hash, code_verifier, nonce_hash, sub, "
        "email, expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s, now() + interval '1 minute')",
        (phase, _hash(60), _hash(61), verifier, nonce, sub, email),
    )


def test_prune_deletes_expired_flows_only(db: Conn) -> None:
    db.execute(
        "INSERT INTO login_flows (phase, flow_hash, binding_hash, sub, email, expires_at) "
        "VALUES ('handle', %s, %s, 's1', 'a@gmail.com', now() - interval '1 second'), "
        "('handle', %s, %s, 's2', 'b@gmail.com', now() + interval '1 minute')",
        (_hash(70), _hash(71), _hash(72), _hash(73)),
    )
    with as_app_rw(db):
        assert scalar(db, "SELECT app_prune_login_flows()") == 1
    assert scalar(db, "SELECT count(*) FROM login_flows WHERE flow_hash = %s", (_hash(72),)) == 1


# ---- the guards ----


@pytest.mark.parametrize(
    "assignment",
    ["google_sub = 'sub-x'", "auth_provider = 'google'", "email_verified_at = now()"],
)
def test_app_rw_update_credential_columns_grd01(db: Conn, assignment: str) -> None:
    user_id = f.user(db)
    set_gucs(db, user_id=user_id)
    with as_app_rw(db):
        statement = f"UPDATE users SET {assignment} WHERE id = %s"  # noqa: S608 - a literal
        expect_sqlstate(db, "GRD01", statement, (user_id,))


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("google_sub", "sub-y"),
        ("auth_provider", "google"),
        ("email_verified_at", datetime.now(UTC)),
    ],
)
def test_app_rw_insert_credential_columns_grd01(db: Conn, column: str, value: Any) -> None:
    email = f"signup-{uuid4().hex[:8]}@x.test"
    set_gucs(db, login_email=email)
    with as_app_rw(db):
        expect_sqlstate(
            db,
            "GRD01",
            f"INSERT INTO users (email, hashed_password, {column}) VALUES (%s, 'h', %s)",  # noqa: S608
            (email, value),
        )


def test_app_rw_password_write_still_passes(db: Conn) -> None:
    user_id = f.user(db)
    set_gucs(db, user_id=user_id)
    with as_app_rw(db):
        db.execute("UPDATE users SET hashed_password = 'new-hash' WHERE id = %s", (user_id,))
    assert scalar(db, "SELECT hashed_password FROM users WHERE id = %s", (user_id,)) == "new-hash"


def test_migrator_writes_credential_columns(db: Conn) -> None:
    user_id = f.user(db)
    with as_migrator(db):
        db.execute("UPDATE users SET google_sub = 'sub-m' WHERE id = %s", (user_id,))
    assert scalar(db, "SELECT google_sub FROM users WHERE id = %s", (user_id,)) == "sub-m"


def test_google_sub_unique_and_bounded(db: Conn) -> None:
    f.user(db, google_sub="dup")
    expect_sqlstate(db, "23505", "INSERT INTO users (email, google_sub, auth_provider) "
                    "VALUES ('d2@x.test', 'dup', 'google')")  # fmt: skip
    expect_sqlstate(db, "23514", "UPDATE users SET google_sub = '' WHERE google_sub = 'dup'")


def test_app_rw_insert_verified_via_grd01(db: Conn) -> None:
    user_id = f.user(db)
    set_gucs(db, user_id=user_id)
    with as_app_rw(db):
        expect_sqlstate(
            db,
            "GRD01",
            "INSERT INTO signup_requests (user_id, brand_name, owner_name, verified_via) "
            "VALUES (%s, 'B', 'O', 'google')",
            (user_id,),
        )
        db.execute(
            "INSERT INTO signup_requests (user_id, brand_name, owner_name) VALUES (%s, 'B', 'O')",
            (user_id,),
        )


def test_verified_via_never_changes(db: Conn) -> None:
    user_id = f.user(db)
    request_id = f.insert(
        db, "signup_requests", user_id=user_id, brand_name="B", owner_name="O",
        verified_via="google",
    )  # fmt: skip
    expect_sqlstate(
        db, "GRD01", "UPDATE signup_requests SET verified_via = NULL WHERE id = %s", (request_id,)
    )
    expect_sqlstate(
        db,
        "23514",
        "INSERT INTO signup_requests (user_id, brand_name, owner_name, verified_via) "
        "VALUES (%s, 'B', 'O', 'apple')",
        (f.user(db),),
    )


# ---- the definers ----


def _applicant(conn: Conn, email: str, sub: str) -> UUID | None:
    set_gucs(conn, login_email=email)
    with as_app_rw(conn):
        found = conn.execute(
            "SELECT app_create_google_applicant(%s, %s, 'Asha Rao', 'Acme Naturals', "
            "'Asha Rao', NULL, NULL)",
            (email, sub),
        ).fetchone()
    assert found is not None
    return found[0]  # type: ignore[no-any-return]


def test_create_applicant_writes_user_and_request(db: Conn) -> None:
    request_id = _applicant(db, "asha@gmail.com", "sub-asha")
    assert request_id is not None
    row = db.execute(
        "SELECT u.hashed_password, u.auth_provider, u.email_verified_at IS NOT NULL, "
        "u.google_sub, u.display_name, r.status, r.verified_via, r.brand_name "
        "FROM signup_requests r JOIN users u ON u.id = r.user_id WHERE r.id = %s",
        (request_id,),
    ).fetchone()
    assert row == (
        None,
        "google",
        True,
        "sub-asha",
        "Asha Rao",
        "pending",
        "google",
        "Acme Naturals",
    )


def test_create_applicant_null_when_email_or_sub_taken(db: Conn) -> None:
    f.user(db, email="taken@gmail.com")
    f.user(db, google_sub="sub-taken", auth_provider="google", hashed_password=None)
    assert _applicant(db, "taken@gmail.com", "sub-new") is None
    assert _applicant(db, "TAKEN@gmail.com", "sub-new") is None
    assert _applicant(db, "fresh@gmail.com", "sub-taken") is None


def test_create_applicant_only_for_login_email(db: Conn) -> None:
    set_gucs(db, login_email="someone@gmail.com")
    with as_app_rw(db):
        expect_sqlstate(
            db,
            "GRD01",
            "SELECT app_create_google_applicant('other@gmail.com', 's', 'n', 'b', 'o', NULL, NULL)",
        )


def test_google_user_finds_live_linked_user(db: Conn) -> None:
    user_id = f.user(db, google_sub="sub-find")
    with as_app_rw(db):
        assert scalar(db, "SELECT app_google_user('sub-find')") == user_id
        assert scalar(db, "SELECT app_google_user('sub-none')") is None
    db.execute("UPDATE users SET erased_at = now() WHERE id = %s", (user_id,))
    with as_app_rw(db):
        assert scalar(db, "SELECT app_google_user('sub-find')") is None


def _link(conn: Conn, user_id: UUID, sub: str, *, as_user: UUID | None = None) -> bool:
    set_gucs(conn, user_id=as_user or user_id)
    with as_app_rw(conn):
        return bool(scalar(conn, "SELECT app_link_google_sub(%s, %s)", (user_id, sub)))


def test_link_only_self_with_password_not_admin(db: Conn) -> None:
    user_id = f.user(db)
    other = f.user(db)
    assert _link(db, user_id, "sub-1", as_user=other) is False
    assert _link(db, user_id, "sub-1") is True
    assert scalar(db, "SELECT google_sub FROM users WHERE id = %s", (user_id,)) == "sub-1"
    assert _link(db, user_id, "sub-2") is False  # already linked: never replaced
    admin = f.user(db, is_platform_admin=True)
    assert _link(db, admin, "sub-3") is False
    no_password = f.user(db, hashed_password=None, auth_provider="google")
    assert _link(db, no_password, "sub-4") is False
    inactive = f.user(db, is_active=False)
    assert _link(db, inactive, "sub-5") is False
    assert _link(db, other, "sub-1") is False  # the Google account is someone else's


def test_unlink_only_self_with_password(db: Conn) -> None:
    user_id = f.user(db, google_sub="sub-u1")
    google_only = f.user(db, google_sub="sub-u2", hashed_password=None, auth_provider="google")
    set_gucs(db, user_id=google_only)
    with as_app_rw(db):
        assert scalar(db, "SELECT app_unlink_google_sub(%s)", (user_id,)) is False
        assert scalar(db, "SELECT app_unlink_google_sub(%s)", (google_only,)) is False
    set_gucs(db, user_id=user_id)
    with as_app_rw(db):
        assert scalar(db, "SELECT app_unlink_google_sub(%s)", (user_id,)) is True
    assert scalar(db, "SELECT google_sub FROM users WHERE id = %s", (user_id,)) is None


# ---- mfa_challenges ----


def _challenge(conn: Conn, user_id: UUID, n: int, amr: list[str], link: str | None) -> None:
    conn.execute(
        "INSERT INTO mfa_challenges (challenge_hash, user_id, purpose, amr, expires_at, "
        "link_google_sub) VALUES (%s, %s, 'login', %s, now() + interval '5 minutes', %s)",
        (_hash(n), user_id, amr, link),
    )


def test_challenge_accepts_fed_and_links_need_a_password(db: Conn) -> None:
    user_id = f.user(db)
    _challenge(db, user_id, 80, ["fed"], None)
    _challenge(db, user_id, 81, ["pwd"], "sub-link")
    expect_sqlstate(
        db,
        "23514",
        "INSERT INTO mfa_challenges (challenge_hash, user_id, purpose, amr, expires_at, "
        "link_google_sub) VALUES (%s, %s, 'login', ARRAY['fed'], now() + interval '5 minutes', "
        "'sub-link')",
        (_hash(82), user_id),
        constraint="ck_mfa_challenges_link_google_sub",
    )
    expect_sqlstate(
        db,
        "23514",
        "INSERT INTO mfa_challenges (challenge_hash, user_id, purpose, amr, expires_at) "
        "VALUES (%s, %s, 'login', ARRAY['sso'], now() + interval '5 minutes')",
        (_hash(83), user_id),
        constraint="ck_mfa_challenges_amr",
    )


def test_refresh_tokens_amr_still_refuses_fed(db: Conn) -> None:
    assert "fed" not in scalar(
        db,
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname = 'ck_refresh_tokens_amr'",
    )


# ---- read-only grants ----


def test_read_only_roles_never_read_google_identity(db: Conn) -> None:
    for role in ("app_ro", "app_platform_ro"):
        db.execute(f"SET ROLE {role}")
        try:
            for query in (
                "SELECT google_sub FROM users",
                "SELECT link_google_sub FROM mfa_challenges",
                "SELECT email FROM login_flows",
                "SELECT sub FROM login_flows",
                "SELECT flow_hash FROM login_flows",
                "SELECT code_verifier FROM login_flows",
            ):
                expect_sqlstate(db, "42501", query)
            db.execute("SELECT id, phase, expires_at, created_at FROM login_flows")
            db.execute("SELECT verified_via FROM signup_requests")
        finally:
            db.execute("RESET ROLE")


# ---- erasure ----


def test_redact_user_clears_google_sub_and_flows(db: Conn) -> None:
    tenant = f.tenant(db)
    user_id = f.member(db, tenant)
    email = scalar(db, "SELECT email FROM users WHERE id = %s", (user_id,))
    db.execute("UPDATE users SET google_sub = 'sub-r' WHERE id = %s", (user_id,))
    db.execute(
        "INSERT INTO login_flows (phase, flow_hash, binding_hash, sub, email, expires_at) "
        "VALUES ('link', %s, %s, 'sub-r', upper(%s), now() + interval '5 minutes')",
        (_hash(90), _hash(91), email),
    )
    with as_migrator(db):
        db.execute("SELECT redact_user(%s)", (user_id,))
    assert scalar(db, "SELECT google_sub FROM users WHERE id = %s", (user_id,)) is None
    assert scalar(db, "SELECT count(*) FROM login_flows WHERE sub = 'sub-r'") == 0


def test_purge_tenant_data_clears_google_of_pseudonymised_users_only(db: Conn) -> None:
    tenant, other_tenant = f.tenant(db), f.tenant(db)
    only_here = f.member(db, tenant)
    also_elsewhere = f.member(db, tenant)
    f.insert(
        db, "tenant_memberships", tenant_id=other_tenant, user_id=also_elsewhere, role="operator"
    )
    for n, user_id in ((100, only_here), (110, also_elsewhere)):
        db.execute("UPDATE users SET google_sub = %s WHERE id = %s", (f"sub-{n}", user_id))
        db.execute(
            "INSERT INTO login_flows (phase, flow_hash, binding_hash, sub, email, expires_at) "
            "SELECT 'link', %s, %s, %s, email, now() + interval '5 minutes' FROM users "
            "WHERE id = %s",
            (_hash(n), _hash(n + 1), f"sub-{n}", user_id),
        )
    with as_migrator(db):
        db.execute("SELECT purge_tenant(%s, 'data', true)", (tenant,))
    assert scalar(db, "SELECT google_sub FROM users WHERE id = %s", (only_here,)) is None
    assert scalar(db, "SELECT google_sub FROM users WHERE id = %s", (also_elsewhere,)) == "sub-110"
    assert scalar(db, "SELECT count(*) FROM login_flows WHERE sub = 'sub-100'") == 0
    assert scalar(db, "SELECT count(*) FROM login_flows WHERE sub = 'sub-110'") == 1
