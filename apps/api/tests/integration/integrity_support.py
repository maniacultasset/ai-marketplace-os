"""Test support for the 0004 guards: role switches, the GUC setter, and row factories over the
`ops_support.seed_graph` two-tenant graph. Each factory inserts on the caller's connection and
transaction and returns the new key; keyword arguments override or add columns.

`as_migrator` changes the *session* user, because the append-only triggers check
`session_user = 'migrator'` (DATABASE.md §5.2 audit_log "Append-only"); `as_app_rw` changes only
`current_user`, which is what privilege checks use."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

import psycopg

Conn = psycopg.Connection[tuple[Any, ...]]


@contextmanager
def as_migrator(conn: Conn) -> Iterator[Conn]:
    """`SET SESSION AUTHORIZATION migrator` for the block (the caller logs in as a superuser)."""
    conn.execute("SET SESSION AUTHORIZATION migrator")
    try:
        yield conn
    finally:
        conn.execute("RESET SESSION AUTHORIZATION")


@contextmanager
def as_app_rw(conn: Conn) -> Iterator[Conn]:
    """`SET ROLE app_rw` for the block: privileges are checked as `app_rw`."""
    conn.execute("SET ROLE app_rw")
    try:
        yield conn
    finally:
        conn.execute("RESET ROLE")


def set_gucs(conn: Conn, **values: object) -> None:
    """`set_config('app.<name>', value, true)` for each keyword (transaction-local); None clears
    the setting to '' (what a pooled connection holds after a reset)."""
    for name, value in values.items():
        conn.execute(
            "SELECT set_config(%s, %s, true)",
            (f"app.{name}", "" if value is None else str(value)),
        )


def scalar(conn: Conn, query: str, params: Any = None) -> Any:
    row = conn.execute(query, params).fetchone()
    assert row is not None
    return row[0]


def uuid_of(value: Any) -> UUID:
    assert isinstance(value, UUID), value
    return value
