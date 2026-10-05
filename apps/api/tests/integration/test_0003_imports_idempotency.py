"""`imports`, `import_rows` and `idempotency_keys` (DATABASE.md §5.4): the idempotency PK and
request digest, the import status vocabulary, the row-number bounds and key, the CASCADE from
imports, `SET NULL (product_id)` that keeps `tenant_id`, and the tenant-composite FKs."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
import pytest
from mkos_schemas.enums import JobState

from . import factories as f
from .ops_support import Seed, digest, idempotency_key, import_row, import_run, job_run, seed_graph

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


# ---- idempotency_keys ----


def test_ops_constraints_idempotency_pk(db: Conn) -> None:
    pk = db.execute(
        "SELECT conname, ARRAY(SELECT a.attname FROM unnest(conkey) WITH ORDINALITY k(n, i) "
        "JOIN pg_attribute a ON a.attrelid = conrelid AND a.attnum = k.n ORDER BY k.i) "
        "FROM pg_constraint WHERE conrelid = 'idempotency_keys'::regclass AND contype = 'p'"
    ).fetchone()
    assert pk == ("pk_idempotency_keys", ["tenant_id", "key"])
    columns = db.execute(
        "SELECT attname FROM pg_attribute WHERE attrelid = 'idempotency_keys'::regclass "
        "AND attnum > 0 AND NOT attisdropped ORDER BY attnum"
    ).fetchall()
    assert [c[0] for c in columns] == [
        "tenant_id",
        "key",
        "endpoint",
        "request_hash",
        "response_status",
        "response_json",
        "user_id",
        "expires_at",
        "created_at",
    ]


def test_ops_constraints_idempotency_replay_key_refused(db: Conn, seed: Seed) -> None:
    idempotency_key(db, seed.a, key="k-1")
    _refused(db, "23505", "pk_idempotency_keys", lambda: idempotency_key(db, seed.a, key="k-1"))
    idempotency_key(db, seed.b, key="k-1")
    # the protocol's first statement: a duplicate is a no-op, not an error
    row = db.execute(
        "INSERT INTO idempotency_keys (tenant_id, key, endpoint, request_hash, user_id, "
        "expires_at) VALUES (%s, 'k-1', 'POST /x', %s, %s, now()) ON CONFLICT DO NOTHING "
        "RETURNING key",
        (seed.a.tenant_id, digest("other"), seed.a.user_id),
    ).fetchone()
    assert row is None


@pytest.mark.parametrize("size", [0, 31, 33, 64])
def test_ops_constraints_request_hash_len(db: Conn, seed: Seed, size: int) -> None:
    _refused(
        db,
        "23514",
        "ck_idempotency_keys_request_hash",
        lambda: idempotency_key(db, seed.a, request_hash=b"\x01" * size),
    )


def test_ops_constraints_request_hash_32_inserts(db: Conn, seed: Seed) -> None:
    idempotency_key(db, seed.a, request_hash=b"\x01" * 32)


def test_ops_cross_tenant_fk_idempotency_user(db: Conn, seed: Seed) -> None:
    _refused(
        db,
        "23503",
        "fk_idempotency_keys_user_id",
        lambda: idempotency_key(db, seed.a, user_id=seed.b.user_id),
    )


def test_idempotency_keys_expires_index(db: Conn) -> None:
    row = db.execute(
        "SELECT pg_get_indexdef('ix_idempotency_keys_expires_at'::regclass)"
    ).fetchone()
    assert row is not None and row[0].endswith("USING btree (expires_at)"), row


# ---- imports ----


@pytest.mark.parametrize("status", [s.value for s in JobState if s.value != "dead_letter"])
def test_imports_status_accepts(db: Conn, seed: Seed, status: str) -> None:
    import_run(db, seed.a, status=status)


@pytest.mark.parametrize("status", ["dead_letter", "QUEUED", "done"])
def test_imports_status_no_dead_letter(db: Conn, seed: Seed, status: str) -> None:
    _refused(db, "23514", "ck_imports_status", lambda: import_run(db, seed.a, status=status))


def test_ops_cross_tenant_fk_imports_job(db: Conn, seed: Seed) -> None:
    other_job = job_run(db, seed.b.tenant_id)
    _refused(db, "23503", "fk_imports_job_id", lambda: import_run(db, seed.a, job_id=other_job))


def test_ops_cross_tenant_fk_imports_created_by(db: Conn, seed: Seed) -> None:
    _refused(
        db,
        "23503",
        "fk_imports_created_by",
        lambda: import_run(db, seed.a, created_by=seed.b.user_id),
    )


def test_imports_job_index(db: Conn) -> None:
    row = db.execute("SELECT pg_get_indexdef('ix_imports_job_id'::regclass)").fetchone()
    assert row is not None and row[0].endswith("USING btree (job_id)"), row


# ---- import_rows ----


def test_import_rows_row_number_min_2(db: Conn, seed: Seed) -> None:
    import_id = import_run(db, seed.a)
    for number in (1, 0, -1):
        _refused(
            db,
            "23514",
            "ck_import_rows_row_number",
            lambda n=number: import_row(db, seed.a.tenant_id, import_id, row_number=n),
        )
    import_row(db, seed.a.tenant_id, import_id, row_number=2)


def test_import_rows_unique_per_import(db: Conn, seed: Seed) -> None:
    import_id = import_run(db, seed.a)
    import_row(db, seed.a.tenant_id, import_id, row_number=2)
    _refused(
        db,
        "23505",
        "uq_import_rows_tenant_id_import_id_row_number",
        lambda: import_row(db, seed.a.tenant_id, import_id, row_number=2),
    )
    import_row(db, seed.a.tenant_id, import_run(db, seed.a), row_number=2)


@pytest.mark.parametrize("status", ["queued", "skipped", ""])
def test_import_rows_status_vocab(db: Conn, seed: Seed, status: str) -> None:
    import_id = import_run(db, seed.a)
    _refused(
        db,
        "23514",
        "ck_import_rows_status",
        lambda: import_row(db, seed.a.tenant_id, import_id, status=status),
    )


def test_import_rows_cascade_on_import_delete(db: Conn, seed: Seed) -> None:
    import_id = import_run(db, seed.a)
    kept = import_run(db, seed.a)
    for number in (2, 3):
        import_row(db, seed.a.tenant_id, import_id, row_number=number)
    import_row(db, seed.a.tenant_id, kept, row_number=2)
    db.execute("DELETE FROM imports WHERE id = %s", (import_id,))
    rows = db.execute("SELECT import_id FROM import_rows WHERE tenant_id = %s", (seed.a.tenant_id,))
    assert rows.fetchall() == [(kept,)]


def test_import_rows_product_set_null_keeps_tenant(db: Conn, seed: Seed) -> None:
    import_id = import_run(db, seed.a)
    product_id = f.product(db, seed.a.tenant_id)
    row_id = import_row(db, seed.a.tenant_id, import_id, product_id=product_id)
    db.execute("DELETE FROM products WHERE id = %s", (product_id,))
    row = db.execute(
        "SELECT tenant_id, import_id, product_id FROM import_rows WHERE id = %s", (row_id,)
    ).fetchone()
    assert row == (seed.a.tenant_id, import_id, None)
    definition = db.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname = 'fk_import_rows_product_id'"
    ).fetchone()
    assert definition == (
        "FOREIGN KEY (tenant_id, product_id) REFERENCES products(tenant_id, id) "
        "ON DELETE SET NULL (product_id)",
    )


def test_ops_cross_tenant_fk_import_rows(db: Conn, seed: Seed) -> None:
    import_id = import_run(db, seed.a)
    _refused(
        db,
        "23503",
        "fk_import_rows_import_id",
        lambda: import_row(db, seed.b.tenant_id, import_id),
    )


def test_ops_cross_tenant_fk_import_rows_product(db: Conn, seed: Seed) -> None:
    import_id = import_run(db, seed.a)
    _refused(
        db,
        "23503",
        "fk_import_rows_product_id",
        lambda: import_row(db, seed.a.tenant_id, import_id, product_id=seed.b.product_id),
    )


def test_import_rows_status_index(db: Conn) -> None:
    row = db.execute(
        "SELECT pg_get_indexdef('ix_import_rows_tenant_id_import_id_status'::regclass)"
    ).fetchone()
    assert row is not None
    assert row[0].endswith("USING btree (tenant_id, import_id, status, row_number)"), row


def test_imports_tenant_delete_cascades(db: Conn, seed: Seed) -> None:
    import_id = import_run(db, seed.a)
    import_row(db, seed.a.tenant_id, import_id)
    idempotency_key(db, seed.a)
    keep: UUID = import_run(db, seed.b)
    db.execute("DELETE FROM tenants WHERE id = %s", (seed.a.tenant_id,))
    for table in ("imports", "import_rows", "idempotency_keys"):
        row = db.execute(
            f"SELECT count(*) FROM {table} WHERE tenant_id = %s",  # noqa: S608 - fixed names
            (seed.a.tenant_id,),
        ).fetchone()
        assert row == (0,), table
    assert db.execute("SELECT id FROM imports").fetchall() == [(keep,)]
