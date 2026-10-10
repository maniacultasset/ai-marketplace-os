"""S26 T13: a lost database connection under a job body is retried by a retrying kind."""

from __future__ import annotations

import psycopg
import pytest
from app.workers.errors import is_transient_db_error
from sqlalchemy.exc import IntegrityError, OperationalError


def _Op(sqlstate: str | None) -> psycopg.Error:  # noqa: N802 - reads like the class it builds
    """The psycopg error class for `sqlstate` (None: a plain OperationalError, as libpq raises
    for a connection that died with no server message)."""
    if sqlstate is None:
        return psycopg.OperationalError("seeded")
    return psycopg.errors.lookup(sqlstate)("seeded")


@pytest.mark.parametrize("state", [None, "08006", "08003", "57P01", "57P02", "57P03"])
def test_transient_db_error_connection_states(state: str | None) -> None:
    assert is_transient_db_error(_Op(state))
    assert is_transient_db_error(OperationalError("SELECT 1", {}, _Op(state)))


def test_transient_db_error_invalidated_connection() -> None:
    error = OperationalError("SELECT 1", {}, Exception("gone"), connection_invalidated=True)
    assert is_transient_db_error(error)


@pytest.mark.parametrize("state", ["57014", "40001", "23505", "42P01"])
def test_transient_db_error_not_for_sql_or_data_errors(state: str) -> None:
    assert not is_transient_db_error(_Op(state))
    assert not is_transient_db_error(IntegrityError("INSERT", {}, Exception("dup")))
    assert not is_transient_db_error(ValueError("x"))
