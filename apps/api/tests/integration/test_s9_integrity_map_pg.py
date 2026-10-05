"""S9 AC 8 (map half): `to_conflict` on IntegrityErrors raised by the real compose PostgreSQL,
never on hand-built diag objects. The session flush wiring (T5) is not built yet."""

from __future__ import annotations

import logging
from typing import Any

import psycopg
import pytest
from mkos_schemas.codes import ErrorCode
from sqlalchemy.exc import IntegrityError

from . import factories as f

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]


def _raise(conn: Conn, build: Any) -> IntegrityError:
    """Run `build` in a savepoint; return the driver error wrapped as SQLAlchemy wraps it."""
    with pytest.raises(psycopg.errors.IntegrityError) as caught, conn.transaction():
        build()
    return IntegrityError("INSERT ...", None, caught.value)


def test_duplicate_sku_maps_to_sku_conflict(db: Conn) -> None:
    from app.db.integrity import to_conflict

    tid = f.tenant(db)
    f.product(db, tid, sku="DUP-1")
    exc = _raise(db, lambda: f.product(db, tid, sku="DUP-1"))
    err = to_conflict(exc)
    assert err.code is ErrorCode.SKU_CONFLICT
    assert err.constraint == "products_tenant_sku_idx"
    assert "DUP-1" not in str(err)


def test_same_sku_other_tenant_is_not_a_conflict(db: Conn) -> None:
    a, b = f.tenant(db), f.tenant(db)
    f.product(db, a, sku="SHARED-1")
    f.product(db, b, sku="SHARED-1")  # (tenant_id, sku) is the key


def test_unmapped_unique_is_internal_error_and_logs_no_values(
    db: Conn, caplog: pytest.LogCaptureFixture
) -> None:
    from app.db.integrity import to_conflict

    secret_slug = "acme-secret-value"
    f.tenant(db, slug=secret_slug)
    exc = _raise(db, lambda: f.tenant(db, slug=secret_slug))
    with caplog.at_level(logging.ERROR, logger="app.db.integrity"):
        err = to_conflict(exc)
    assert err.code is ErrorCode.INTERNAL_ERROR
    assert err.constraint != "<none>"
    assert secret_slug not in str(err)
    assert secret_slug not in caplog.text
    assert "23505" in caplog.text


def test_not_null_violation_has_no_constraint_and_maps_internal(db: Conn) -> None:
    from app.db.integrity import to_conflict

    tid = f.tenant(db)
    exc = _raise(db, lambda: f.product(db, tid, sku=None))
    err = to_conflict(exc)
    assert err.code is ErrorCode.INTERNAL_ERROR
