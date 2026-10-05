"""`refresh_tokens` (DATABASE.md §5.1), `oauth_states` and `rate_limit_counters` (§5.4): the
membership FKs behind the invariant-5 exceptions, rotation that never links across tenants,
`SET NULL (replaced_by)`, consume-once states, the token and state digests, and the UNLOGGED,
tenant-less counters."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import psycopg
import pytest

from . import factories as f
from .ops_support import Seed, digest, oauth_state, refresh_token, seed_graph

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]


@pytest.fixture
def seed(db: Conn) -> Seed:
    return seed_graph(db)


def _refused(db: Conn, code: str, constraint: str, build: Any) -> None:
    try:
        with db.transaction():
            build()
    except psycopg.Error as error:
        assert error.sqlstate == code, error
        assert error.diag.constraint_name == constraint, error.diag.constraint_name
    else:
        raise AssertionError(f"wanted {code} on {constraint}, the insert succeeded")


# ---- rate_limit_counters ----


def test_rate_limit_counters_unlogged(db: Conn) -> None:
    row = db.execute(
        "SELECT relpersistence FROM pg_class WHERE oid = 'rate_limit_counters'::regclass"
    ).fetchone()
    assert row == ("u",)
    columns = db.execute(
        "SELECT attname, format_type(atttypid, atttypmod), attnotnull FROM pg_attribute "
        "WHERE attrelid = 'rate_limit_counters'::regclass AND attnum > 0 AND NOT attisdropped "
        "ORDER BY attnum"
    ).fetchall()
    assert columns == [
        ("bucket_key", "text", True),
        ("window_start", "timestamp with time zone", True),
        ("n", "integer", True),
    ]
    pk = db.execute(
        "SELECT conname, ARRAY(SELECT a.attname FROM unnest(conkey) WITH ORDINALITY k(n, i) "
        "JOIN pg_attribute a ON a.attrelid = conrelid AND a.attnum = k.n ORDER BY k.i) "
        "FROM pg_constraint WHERE conrelid = 'rate_limit_counters'::regclass AND contype = 'p'"
    ).fetchone()
    assert pk == ("pk_rate_limit_counters", ["bucket_key", "window_start"])


def test_rate_limit_counters_upsert_increments(db: Conn) -> None:
    upsert = (
        "INSERT INTO rate_limit_counters (bucket_key, window_start, n) "
        "VALUES (%s, date_trunc('minute', now()), 1) "
        "ON CONFLICT (bucket_key, window_start) DO UPDATE SET n = rate_limit_counters.n + 1 "
        "RETURNING n"
    )
    bucket = f"t:{uuid4()}"
    assert db.execute(upsert, (bucket,)).fetchone() == (1,)
    assert db.execute(upsert, (bucket,)).fetchone() == (2,)
    assert db.execute(upsert, (f"ip:{digest('x').hex()}",)).fetchone() == (1,)


# ---- refresh_tokens ----


def test_ops_cross_tenant_fk_refresh_replaced_by(db: Conn, seed: Seed) -> None:
    successor = refresh_token(db, seed.b)
    _refused(
        db,
        "23503",
        "fk_refresh_tokens_replaced_by",
        lambda: refresh_token(db, seed.a, replaced_by=successor),
    )
    refresh_token(db, seed.a, replaced_by=refresh_token(db, seed.a))


def test_refresh_tokens_replaced_by_set_null(db: Conn, seed: Seed) -> None:
    successor = refresh_token(db, seed.a)
    rotated = refresh_token(db, seed.a, replaced_by=successor, revoked_reason="rotated")
    db.execute("DELETE FROM refresh_tokens WHERE id = %s", (successor,))
    row = db.execute(
        "SELECT tenant_id, user_id, replaced_by, revoked_reason FROM refresh_tokens WHERE id = %s",
        (rotated,),
    ).fetchone()
    assert row == (seed.a.tenant_id, seed.a.user_id, None, "rotated")


def test_refresh_tokens_membership_cascade(db: Conn, seed: Seed) -> None:
    # a member who holds only tokens: deleting the membership deletes them (the CASCADE exception)
    lone = f.member(db, seed.a.tenant_id)
    gone = refresh_token(db, seed.a, user_id=lone)
    kept = refresh_token(db, seed.a)
    other = refresh_token(db, seed.b)
    db.execute(
        "DELETE FROM tenant_memberships WHERE tenant_id = %s AND user_id = %s",
        (seed.a.tenant_id, lone),
    )
    remaining = {r[0] for r in db.execute("SELECT id FROM refresh_tokens").fetchall()}
    assert gone not in remaining
    assert {kept, other} <= remaining


def test_refresh_tokens_membership_fk_is_cascade(db: Conn) -> None:
    rows = db.execute(
        "SELECT conname, confdeltype FROM pg_constraint WHERE conrelid = 'refresh_tokens'::regclass "
        "AND contype = 'f' ORDER BY 1"
    ).fetchall()
    assert rows == [
        ("fk_refresh_tokens_replaced_by", "n"),
        ("fk_refresh_tokens_tenant_id_tenants", "c"),
        ("fk_refresh_tokens_user_id", "c"),
    ]
    definition = db.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname = 'fk_refresh_tokens_replaced_by'"
    ).fetchone()
    assert definition == (
        "FOREIGN KEY (tenant_id, replaced_by) REFERENCES refresh_tokens(tenant_id, id) "
        "ON DELETE SET NULL (replaced_by)",
    )


def test_refresh_tokens_token_hash_unique(db: Conn, seed: Seed) -> None:
    refresh_token(db, seed.a, token_hash=digest("same"))
    # globally unique: the pre-tenant lookup by hash must find one row
    _refused(
        db,
        "23505",
        "uq_refresh_tokens_token_hash",
        lambda: refresh_token(db, seed.b, token_hash=digest("same")),
    )


def test_ops_cross_tenant_fk_refresh_user(db: Conn, seed: Seed) -> None:
    _refused(
        db,
        "23503",
        "fk_refresh_tokens_user_id",
        lambda: refresh_token(db, seed.a, user_id=seed.b.user_id),
    )


# ---- oauth_states ----


def test_oauth_states_consume_once(db: Conn, seed: Seed) -> None:
    state = digest("state-1")
    oauth_state(db, seed.a, state_hash=state, code_verifier="v" * 64)
    consume = (
        "DELETE FROM oauth_states WHERE state_hash = %s AND expires_at > now() "
        "RETURNING tenant_id, user_id, marketplace, code_verifier"
    )
    assert db.execute(consume, (state,)).fetchall() == [
        (seed.a.tenant_id, seed.a.user_id, "amazon", "v" * 64)
    ]
    assert db.execute(consume, (state,)).fetchall() == []


def test_oauth_states_expired_not_consumed(db: Conn, seed: Seed) -> None:
    state = digest("state-old")
    oauth_state(db, seed.a, state_hash=state, expires_at="2000-01-01T00:00:00Z")
    rows = db.execute(
        "DELETE FROM oauth_states WHERE state_hash = %s AND expires_at > now() RETURNING id",
        (state,),
    ).fetchall()
    assert rows == []


def test_oauth_states_marketplace_check(db: Conn, seed: Seed) -> None:
    _refused(
        db,
        "23514",
        "ck_oauth_states_marketplace",
        lambda: oauth_state(db, seed.a, marketplace="ebay"),
    )


def test_oauth_states_state_hash_unique(db: Conn, seed: Seed) -> None:
    oauth_state(db, seed.a, state_hash=digest("dup"))
    _refused(
        db,
        "23505",
        "uq_oauth_states_state_hash",
        lambda: oauth_state(db, seed.b, state_hash=digest("dup")),
    )


def test_ops_cross_tenant_fk_oauth_user(db: Conn, seed: Seed) -> None:
    _refused(
        db,
        "23503",
        "fk_oauth_states_user_id",
        lambda: oauth_state(db, seed.a, user_id=seed.b.user_id),
    )


# ---- digests ----


@pytest.mark.parametrize("size", [0, 31, 33])
def test_ops_constraints_token_and_state_hash_len(db: Conn, seed: Seed, size: int) -> None:
    value = b"\x02" * size
    _refused(
        db,
        "23514",
        "ck_refresh_tokens_token_hash",
        lambda: refresh_token(db, seed.a, token_hash=value),
    )
    _refused(
        db,
        "23514",
        "ck_oauth_states_state_hash",
        lambda: oauth_state(db, seed.a, state_hash=value),
    )


def test_auth_tables_tenant_delete_cascades(db: Conn, seed: Seed) -> None:
    refresh_token(db, seed.a, replaced_by=refresh_token(db, seed.a))
    oauth_state(db, seed.a)
    oauth_state(db, seed.b)
    db.execute("DELETE FROM tenants WHERE id = %s", (seed.a.tenant_id,))
    for table in ("refresh_tokens", "oauth_states"):
        row = db.execute(
            f"SELECT count(*) FROM {table} WHERE tenant_id = %s",  # noqa: S608 - fixed names
            (seed.a.tenant_id,),
        ).fetchone()
        assert row == (0,), table
