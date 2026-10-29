"""Revision 0011_mfa_lockout (DPP-1, ADR-0021; CODE-PLAN dpp-1 T8, §4): the round trip, the
catalog, the lockout functions (counts 9, 10, 11, expiry, the row-lock gate), the guards, the
read-only grants and the erasure redefinitions. Run in the Docker batch (`make test-db`,
`make api-tests`)."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from . import factories as f
from .conftest import ScratchDb, expect_sqlstate, head_database
from .integrity_support import as_app_rw, as_migrator, scalar, set_gucs

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]
MakeConfig = Callable[[Path | None], Config]

PREVIOUS = "0010_signup_requests"
REVISION = "0011_mfa_lockout"
MFA_TABLES = ("user_mfa_factors", "user_recovery_codes", "mfa_challenges", "user_password_history")
NEW_USER_COLUMNS = (
    "failed_auth_count",
    "locked_until",
    "mfa_last_changed_at",
    "password_changed_at",
    "password_must_change",
)
ERASURE_FUNCTIONS = ("public.redact_user(uuid)", "public.purge_tenant(uuid,text,boolean)")
HASH = bytes(range(32))


def _hash(n: int) -> bytes:
    return bytes([n % 256]) * 32


def _fingerprint(db: ScratchDb) -> dict[str, Any]:
    with psycopg.connect(db.libpq, autocommit=True) as conn:
        bodies = {
            fn: scalar(conn, "SELECT md5(pg_get_functiondef(%s::regprocedure))", (fn,))
            for fn in ERASURE_FUNCTIONS
        }
        tables = conn.execute(
            "SELECT relname FROM pg_class WHERE relnamespace = 'public'::regnamespace "
            "AND relname = ANY(%s)",
            (list(MFA_TABLES),),
        ).fetchall()
        columns = conn.execute(
            "SELECT attname FROM pg_attribute WHERE attrelid = 'public.users'::regclass "
            "AND attname = ANY(%s) AND NOT attisdropped",
            (list(NEW_USER_COLUMNS),),
        ).fetchall()
    return {"bodies": bodies, "tables": sorted(tables), "columns": sorted(columns)}


# ---- round trip ----


def test_round_trip_up_down_up(alembic_env: MakeConfig, scratch_db: ScratchDb) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, PREVIOUS)
    before = _fingerprint(scratch_db)
    assert before["tables"] == [] and before["columns"] == []
    command.upgrade(cfg, REVISION)
    at_head = _fingerprint(scratch_db)
    assert len(at_head["tables"]) == 4 and len(at_head["columns"]) == 5
    assert at_head["bodies"] != before["bodies"]
    # `alembic check` needs the head; 0011b adds no table or column
    command.upgrade(cfg, "head")
    command.check(cfg)
    command.downgrade(cfg, PREVIOUS)
    # test_downgrade_restores_0004_erasure_bodies: the downgrade puts back the 0004 bodies
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


def test_lock_timeout_5s_both_directions(alembic_env: MakeConfig) -> None:
    alembic_env(None)
    up = _offline_sql("upgrade", f"{PREVIOUS}:{REVISION}")
    down = _offline_sql("downgrade", f"{REVISION}:{PREVIOUS}")
    for text in (up, down):
        assert text.index("SET lock_timeout = '5s'") < text.index("ALTER TABLE")


def test_checks_added_not_valid_then_validated(alembic_env: MakeConfig, db: Conn) -> None:
    alembic_env(None)
    up = _offline_sql("upgrade", f"{PREVIOUS}:{REVISION}")
    for name, table in (
        ("ck_users_failed_auth_count", "users"),
        ("ck_refresh_tokens_amr", "refresh_tokens"),
    ):
        added = up.index(f"ADD CONSTRAINT {name}")
        assert "NOT VALID" in up[added : up.index(";", added)]
        assert up.index(f"ALTER TABLE public.{table} VALIDATE CONSTRAINT {name}") > added
        assert scalar(db, "SELECT convalidated FROM pg_constraint WHERE conname = %s", (name,))


# ---- catalog ----


def test_catalog_tables_columns_checks_indexes(db: Conn) -> None:
    for table in MFA_TABLES:
        assert scalar(db, "SELECT to_regclass(%s) IS NOT NULL", (f"public.{table}",))
    indexes = {
        r[0]
        for r in db.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' AND tablename = ANY(%s)",
            (list(MFA_TABLES),),
        ).fetchall()
    }
    assert {
        "uq_user_mfa_factors_active",
        "uq_user_mfa_factors_pending",
        "uq_user_recovery_codes_code_hash",
        "ix_user_recovery_codes_user_id",
        "uq_mfa_challenges_challenge_hash",
        "ix_mfa_challenges_user_id",
        "ix_user_password_history_user_id_created_at",
    } <= indexes
    checks = {
        r[0]
        for r in db.execute(
            "SELECT conname FROM pg_constraint WHERE contype = 'c' AND conrelid = ANY("
            "ARRAY['public.user_mfa_factors', 'public.user_recovery_codes', "
            "'public.mfa_challenges', 'public.users', 'public.refresh_tokens']::regclass[])"
        ).fetchall()
    }
    assert {
        "ck_user_mfa_factors_kind",
        "ck_user_mfa_factors_status",
        "ck_user_mfa_factors_secret_blob",
        "ck_mfa_challenges_purpose",
        "ck_mfa_challenges_amr",
        "ck_mfa_challenges_attempts",
        "ck_mfa_challenges_mfa_at",
        "ck_user_recovery_codes_code_hash",
        "ck_users_failed_auth_count",
        "ck_refresh_tokens_amr",
    } <= checks
    for column in ("amr", "mfa_at"):
        assert (
            scalar(
                db,
                "SELECT count(*) FROM pg_attribute WHERE attrelid = 'public.refresh_tokens'::regclass "
                "AND attname = %s AND NOT attisdropped",
                (column,),
            )
            == 1
        )


def test_rls_enabled_and_forced_on_4_tables(db: Conn) -> None:
    rows = db.execute(
        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
        "WHERE relnamespace = 'public'::regnamespace AND relname = ANY(%s)",
        (list(MFA_TABLES),),
    ).fetchall()
    assert sorted(rows) == sorted((t, True, True) for t in MFA_TABLES)


def test_password_changed_at_backfilled_no_rewrite(
    alembic_env: MakeConfig, scratch_db: ScratchDb
) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, PREVIOUS)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        user_id = f.user(conn)
        node = scalar(conn, "SELECT pg_relation_filenode('public.users')")
    command.upgrade(cfg, REVISION)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert scalar(conn, "SELECT pg_relation_filenode('public.users')") == node
        row = conn.execute(
            "SELECT failed_auth_count, locked_until, password_must_change, "
            "password_changed_at > now() - interval '1 hour' FROM users WHERE id = %s",
            (user_id,),
        ).fetchone()
    assert row == (0, None, False, True)


# ---- the lockout functions ----


def _note(conn: Conn, user_id: UUID) -> bool:
    return bool(scalar(conn, "SELECT app_note_auth_failure(%s)", (user_id,)))


def _state(conn: Conn, user_id: UUID) -> tuple[int, bool]:
    row = conn.execute(
        "SELECT failed_auth_count, locked_until > now() FROM users WHERE id = %s", (user_id,)
    ).fetchone()
    assert row is not None
    return int(row[0]), bool(row[1])


def test_note_failure_counts_and_locks(db: Conn) -> None:
    user_id = f.user(db)
    with as_app_rw(db):
        assert [_note(db, user_id) for _ in range(9)] == [False] * 9
    assert db.execute(
        "SELECT failed_auth_count, locked_until FROM users WHERE id = %s", (user_id,)
    ).fetchone() == (9, None)


def test_lock_counts_9_10_11(db: Conn) -> None:
    user_id = f.user(db)
    with as_app_rw(db):
        for _ in range(9):
            _note(db, user_id)
    assert _state(db, user_id) == (9, False)
    with as_app_rw(db):
        assert _note(db, user_id) is True  # the 10th locks and resets the count
    assert _state(db, user_id) == (0, True)
    with as_app_rw(db):
        assert _note(db, user_id) is False  # an 11th while locked is a no-op
    assert _state(db, user_id) == (0, True)
    seconds = scalar(
        db, "SELECT extract(epoch FROM locked_until - now()) FROM users WHERE id = %s", (user_id,)
    )
    assert 890 < float(seconds) <= 900


def test_note_failure_ignored_while_locked(db: Conn) -> None:
    user_id = f.user(db)
    db.execute(
        "UPDATE users SET locked_until = now() + interval '1 minute' WHERE id = %s", (user_id,)
    )
    with as_app_rw(db):
        assert _note(db, user_id) is False
    assert _state(db, user_id) == (0, True)


def test_note_failure_after_expiry_starts_fresh(db: Conn) -> None:
    user_id = f.user(db)
    db.execute(
        "UPDATE users SET locked_until = now() - interval '1 second' WHERE id = %s", (user_id,)
    )
    with as_app_rw(db):
        assert _note(db, user_id) is False
    assert db.execute(
        "SELECT failed_auth_count, locked_until FROM users WHERE id = %s", (user_id,)
    ).fetchone() == (1, None)


def test_clear_failures_only_for_app_user_id(db: Conn) -> None:
    mine, other = f.user(db), f.user(db)
    db.execute("UPDATE users SET failed_auth_count = 4 WHERE id IN (%s, %s)", (mine, other))
    set_gucs(db, user_id=mine)
    with as_app_rw(db):
        db.execute("SELECT app_clear_auth_failures(%s)", (other,))
        db.execute("SELECT app_clear_auth_failures(%s)", (mine,))
    assert scalar(db, "SELECT failed_auth_count FROM users WHERE id = %s", (mine,)) == 0
    assert scalar(db, "SELECT failed_auth_count FROM users WHERE id = %s", (other,)) == 4


def test_begin_auth_attempt_unknown_user_returns_no_row(db: Conn) -> None:
    with as_app_rw(db):
        assert db.execute("SELECT * FROM app_begin_auth_attempt(%s)", (uuid4(),)).fetchall() == []


def test_begin_auth_attempt_returns_lock_fields(db: Conn) -> None:
    user_id = f.user(db)
    db.execute("UPDATE users SET failed_auth_count = 3 WHERE id = %s", (user_id,))
    with as_app_rw(db):
        row = db.execute("SELECT * FROM app_begin_auth_attempt(%s)", (user_id,)).fetchone()
    assert row == (3, None)


def test_only_app_rw_executes_the_definers(db: Conn) -> None:
    user_id = f.user(db)
    for role in ("app_ro", "app_platform_ro"):
        db.execute(f"SET ROLE {role}")
        try:
            for fn in (
                "app_begin_auth_attempt",
                "app_note_auth_failure",
                "app_clear_auth_failures",
            ):
                expect_sqlstate(db, "42501", f"SELECT {fn}(%s)", (user_id,))
        finally:
            db.execute("RESET ROLE")


# ---- the guards ----


def test_app_rw_signup_insert_passes_guard(db: Conn) -> None:
    email = f"signup-{uuid4().hex[:8]}@x.test"
    set_gucs(db, login_email=email)
    with as_app_rw(db):
        f.user(db, email=email)
    assert scalar(db, "SELECT failed_auth_count FROM users WHERE lower(email) = %s", (email,)) == 0


def test_app_rw_insert_with_nonzero_count_grd01(db: Conn) -> None:
    email = f"signup-{uuid4().hex[:8]}@x.test"
    set_gucs(db, login_email=email)
    with as_app_rw(db):
        expect_sqlstate(
            db,
            "GRD01",
            "INSERT INTO users (email, hashed_password, failed_auth_count) VALUES (%s, 'h', 3)",
            (email,),
        )
        expect_sqlstate(
            db,
            "GRD01",
            "INSERT INTO users (email, hashed_password, locked_until) VALUES (%s, 'h', now())",
            (email,),
        )


def test_app_rw_update_other_columns_passes_guard(db: Conn) -> None:
    user_id = f.user(db)
    set_gucs(db, user_id=user_id)
    with as_app_rw(db):
        db.execute(
            "UPDATE users SET last_login_at = now(), display_name = 'New', "
            "password_must_change = true WHERE id = %s",
            (user_id,),
        )
    assert scalar(db, "SELECT display_name FROM users WHERE id = %s", (user_id,)) == "New"


def test_app_rw_direct_write_lock_columns_grd01(db: Conn) -> None:
    user_id = f.user(db)
    set_gucs(db, user_id=user_id)
    with as_app_rw(db):
        expect_sqlstate(
            db, "GRD01", "UPDATE users SET failed_auth_count = 1 WHERE id = %s", (user_id,)
        )
        expect_sqlstate(
            db, "GRD01", "UPDATE users SET locked_until = now() WHERE id = %s", (user_id,)
        )


def _factor(conn: Conn, user_id: UUID, **columns: Any) -> UUID:
    defaults: dict[str, Any] = {
        "user_id": user_id,
        "secret_blob": b"MFA1-sealed",
        "key_version": "local-1",
    }
    return f.insert(conn, "user_mfa_factors", **{**defaults, **columns})  # type: ignore[no-any-return]


def test_app_rw_insert_active_factor_grd01(db: Conn) -> None:
    user_id = f.user(db)
    set_gucs(db, user_id=user_id)
    with as_app_rw(db):
        expect_sqlstate(
            db,
            "GRD01",
            "INSERT INTO user_mfa_factors (user_id, status, secret_blob, key_version) "
            "VALUES (%s, 'active', 'x', 'local-1')",
            (user_id,),
        )
        expect_sqlstate(
            db,
            "GRD01",
            "INSERT INTO user_mfa_factors (user_id, secret_blob, key_version, last_used_step) "
            "VALUES (%s, 'x', 'local-1', 5)",
            (user_id,),
        )
        _factor(db, user_id)


def test_app_rw_update_secret_blob_grd01(db: Conn) -> None:
    user_id = f.user(db)
    set_gucs(db, user_id=user_id)
    with as_app_rw(db):
        factor = _factor(db, user_id)
        for assignment in ("secret_blob = 'other'", "key_version = 'local-2'"):
            expect_sqlstate(
                db,
                "GRD01",
                f"UPDATE user_mfa_factors SET {assignment} WHERE id = %s",  # noqa: S608 - constants
                (factor,),
            )
        db.execute(
            "UPDATE user_mfa_factors SET status = 'active', confirmed_at = now() WHERE id = %s",
            (factor,),
        )


def test_last_used_step_cannot_decrease_grd01(db: Conn) -> None:
    user_id = f.user(db)
    set_gucs(db, user_id=user_id)
    with as_app_rw(db):
        factor = _factor(db, user_id)
        db.execute("UPDATE user_mfa_factors SET last_used_step = 100 WHERE id = %s", (factor,))
        db.execute("UPDATE user_mfa_factors SET last_used_step = 101 WHERE id = %s", (factor,))
        expect_sqlstate(
            db, "GRD01", "UPDATE user_mfa_factors SET last_used_step = 99 WHERE id = %s", (factor,)
        )
        expect_sqlstate(
            db,
            "GRD01",
            "UPDATE user_mfa_factors SET last_used_step = NULL WHERE id = %s",
            (factor,),
        )


def test_challenge_mfa_at_tied_to_amr(db: Conn) -> None:
    user_id = f.user(db)
    base: dict[str, Any] = {
        "user_id": user_id,
        "purpose": "login",
        "expires_at": "2099-01-01T00:00:00Z",
    }
    expect_sqlstate(
        db,
        "23514",
        "INSERT INTO mfa_challenges (user_id, purpose, expires_at, challenge_hash, amr, mfa_at) "
        "VALUES (%s, 'login', '2099-01-01', %s, ARRAY['pwd'], now())",
        (user_id, _hash(1)),
    )
    expect_sqlstate(
        db,
        "23514",
        "INSERT INTO mfa_challenges (user_id, purpose, expires_at, challenge_hash, amr) "
        "VALUES (%s, 'password_change', '2099-01-01', %s, ARRAY['pwd', 'otp'])",
        (user_id, _hash(2)),
    )
    f.insert(db, "mfa_challenges", challenge_hash=_hash(3), amr=["pwd"], **base)


def test_read_only_roles_cannot_select_secret_or_hashes(db: Conn) -> None:
    for role in ("app_ro", "app_platform_ro"):
        db.execute(f"SET ROLE {role}")
        try:
            for query in (
                "SELECT secret_blob FROM user_mfa_factors",
                "SELECT code_hash FROM user_recovery_codes",
                "SELECT challenge_hash FROM mfa_challenges",
                "SELECT ip FROM mfa_challenges",
                "SELECT hashed_password FROM user_password_history",
                "SELECT id FROM user_password_history",
                "SELECT hashed_password FROM users",
            ):
                expect_sqlstate(db, "42501", query)
            db.execute("SELECT id, status, key_version FROM user_mfa_factors")
            db.execute("SELECT failed_auth_count, locked_until, password_changed_at FROM users")
            db.execute("SELECT amr, mfa_at FROM refresh_tokens")
        finally:
            db.execute("RESET ROLE")


def test_app_rw_no_truncate(db: Conn) -> None:
    with as_app_rw(db):
        for table in MFA_TABLES:
            expect_sqlstate(db, "42501", f"TRUNCATE {table}")


def test_app_rw_cannot_update_password_history(db: Conn) -> None:
    user_id = f.user(db)
    set_gucs(db, user_id=user_id)
    with as_app_rw(db):
        f.insert(db, "user_password_history", user_id=user_id, hashed_password="old")
        expect_sqlstate(
            db,
            "42501",
            "UPDATE user_password_history SET hashed_password = 'x' WHERE user_id = %s",
            (user_id,),
        )


# ---- erasure ----


def _mfa_rows(conn: Conn, user_id: UUID) -> int:
    total = 0
    for table in MFA_TABLES:
        query = f"SELECT count(*) FROM {table} WHERE user_id = %s"  # noqa: S608 - constants
        total += int(scalar(conn, query, (user_id,)))
    return total


def _seed_mfa(conn: Conn, user_id: UUID, n: int) -> None:
    _factor(conn, user_id)
    f.insert(conn, "user_recovery_codes", user_id=user_id, code_hash=_hash(n))
    f.insert(
        conn,
        "mfa_challenges",
        user_id=user_id,
        challenge_hash=_hash(n + 100),
        purpose="login",
        amr=["pwd"],
        expires_at="2099-01-01T00:00:00Z",
        ip="10.0.0.9",
        user_agent="ua",
    )
    f.insert(conn, "user_password_history", user_id=user_id, hashed_password="old")


def test_redact_user_clears_mfa_rows(db: Conn) -> None:
    tenant = f.tenant(db)
    user_id = f.member(db, tenant)
    _seed_mfa(db, user_id, 1)
    db.execute(
        "UPDATE users SET failed_auth_count = 3, locked_until = now() + interval '1 minute', "
        "password_must_change = true WHERE id = %s",
        (user_id,),
    )
    assert _mfa_rows(db, user_id) == 4
    with as_migrator(db):
        db.execute("SELECT redact_user(%s)", (user_id,))
    assert _mfa_rows(db, user_id) == 0
    assert db.execute(
        "SELECT failed_auth_count, locked_until, password_must_change FROM users WHERE id = %s",
        (user_id,),
    ).fetchone() == (0, None, False)


def test_purge_tenant_data_clears_mfa_rows_of_pseudonymised_users_only(db: Conn) -> None:
    tenant, other_tenant = f.tenant(db), f.tenant(db)
    only_here = f.member(db, tenant)
    also_elsewhere = f.member(db, tenant)
    f.insert(
        db, "tenant_memberships", tenant_id=other_tenant, user_id=also_elsewhere, role="operator"
    )
    _seed_mfa(db, only_here, 10)
    _seed_mfa(db, also_elsewhere, 20)
    with as_migrator(db):
        db.execute("SELECT purge_tenant(%s, 'data', true)", (tenant,))
    assert _mfa_rows(db, only_here) == 0
    assert _mfa_rows(db, also_elsewhere) == 4


def test_purge_tenant_final_cascades_mfa_rows(db: Conn) -> None:
    tenant = f.tenant(db)
    user_id = f.member(db, tenant)
    _seed_mfa(db, user_id, 30)
    with as_migrator(db):
        db.execute("SELECT purge_tenant(%s, 'data', true)", (tenant,))
        db.execute("SELECT purge_tenant(%s, 'final', true)", (tenant,))
    assert scalar(db, "SELECT count(*) FROM users WHERE id = %s", (user_id,)) == 0
    assert _mfa_rows(db, user_id) == 0


# ---- concurrency (committed rows on a dedicated head database) ----


@pytest.fixture(scope="module")
def lock_db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ScratchDb]:
    with head_database(tmp_path_factory.mktemp("mfa_lockout")) as db:
        yield db


def _connect(db: ScratchDb, *, autocommit: bool = False) -> Conn:
    return psycopg.connect(db.libpq, autocommit=autocommit, connect_timeout=5)


@pytest.fixture
def committed_users(lock_db: ScratchDb) -> Iterator[tuple[UUID, UUID]]:
    with _connect(lock_db) as conn:
        a, b = f.user(conn), f.user(conn)
        conn.commit()
    yield a, b
    with _connect(lock_db, autocommit=True) as conn:
        conn.execute("DELETE FROM users WHERE id IN (%s, %s)", (a, b))


def _wait_until_blocked(db: ScratchDb, pid: int) -> None:
    with _connect(db, autocommit=True) as probe:
        for _ in range(100):
            row = probe.execute(
                "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", (pid,)
            ).fetchone()
            if row is not None and row[0] == "Lock":
                return
            time.sleep(0.05)
    raise AssertionError(f"backend {pid} never waited on a lock")


def test_begin_auth_attempt_blocks_second_connection(
    lock_db: ScratchDb, committed_users: tuple[UUID, UUID]
) -> None:
    user_id, _ = committed_users
    with _connect(lock_db) as first, _connect(lock_db) as second:
        first.execute("SET ROLE app_rw")
        first.execute("SELECT * FROM app_begin_auth_attempt(%s)", (user_id,)).fetchall()
        pid = int(scalar(second, "SELECT pg_backend_pid()"))
        second.execute("SET ROLE app_rw")
        with ThreadPoolExecutor(1) as pool:
            waiting = pool.submit(
                lambda: second.execute(
                    "SELECT * FROM app_begin_auth_attempt(%s)", (user_id,)
                ).fetchone()
            )
            _wait_until_blocked(lock_db, pid)
            first.execute("SELECT app_note_auth_failure(%s)", (user_id,))
            first.commit()
            assert waiting.result(timeout=10) == (1, None)
        second.rollback()


def test_begin_auth_attempt_other_user_not_blocked(
    lock_db: ScratchDb, committed_users: tuple[UUID, UUID]
) -> None:
    a, b = committed_users
    with _connect(lock_db) as first, _connect(lock_db) as second:
        first.execute("SET ROLE app_rw")
        first.execute("SELECT * FROM app_begin_auth_attempt(%s)", (a,)).fetchall()
        second.execute("SET ROLE app_rw")
        started = time.monotonic()
        assert second.execute("SELECT * FROM app_begin_auth_attempt(%s)", (b,)).fetchone() == (
            0,
            None,
        )
        assert time.monotonic() - started < 2
        first.rollback()
        second.rollback()


def test_begin_auth_attempt_lock_timeout_55p03(
    lock_db: ScratchDb, committed_users: tuple[UUID, UUID]
) -> None:
    user_id, _ = committed_users
    with _connect(lock_db) as first, _connect(lock_db) as second:
        first.execute("SET ROLE app_rw")
        first.execute("SELECT * FROM app_begin_auth_attempt(%s)", (user_id,)).fetchall()
        second.execute("SET ROLE app_rw")
        started = time.monotonic()
        with pytest.raises(psycopg.errors.LockNotAvailable):
            second.execute("SELECT * FROM app_begin_auth_attempt(%s)", (user_id,)).fetchall()
        assert 4 < time.monotonic() - started < 9
        first.rollback()
        second.rollback()


def test_note_failure_concurrent_10_calls_lock_once(
    lock_db: ScratchDb, committed_users: tuple[UUID, UUID]
) -> None:
    user_id, _ = committed_users
    with _connect(lock_db, autocommit=True) as conn:
        conn.execute("UPDATE users SET failed_auth_count = 8 WHERE id = %s", (user_id,))

    def one() -> bool:
        with _connect(lock_db, autocommit=True) as conn:
            conn.execute("SET ROLE app_rw")
            return bool(scalar(conn, "SELECT app_note_auth_failure(%s)", (user_id,)))

    with ThreadPoolExecutor(10) as pool:
        results = list(pool.map(lambda _: one(), range(10)))
    assert results.count(True) == 1
    with _connect(lock_db, autocommit=True) as conn:
        assert _state(conn, user_id) == (0, True)
