"""One test database per pytest-xdist worker (`make api-tests -n`).

`make api-tests` migrates `mos_test`, checks it and syncs the attribute dictionary; under xdist
each worker `gwN` then runs against its own copy, `mos_test_gwN`, made with
`CREATE DATABASE … TEMPLATE mos_test` (a file copy: schema, rows, grants and the dictionary rows
come with it; roles are cluster-wide). The controller drops any leftover copies before the
workers start and drops this run's copies at the end; a worker drops a stale copy of its own name
before it creates it. A serial run (no `-n`) and every other database are left alone.

Nothing here imports `app`: the worker's DATABASE_URL is pointed at its copy before any settings
or engine exist."""

from __future__ import annotations

import os
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg import sql

TEMPLATE = "mos_test"
WORKER_DB = re.compile(r"mos_test_gw[0-9]+")
_WORKER_ID = re.compile(r"gw[0-9]+")
_URL = re.compile(r"^(?P<base>[a-z+]+://[^/]*)/(?P<db>[A-Za-z0-9_]+)$")
_IN_USE = "55006"  # object_in_use: the template had a session open for a moment
# pg_advisory_lock key for `role_catalog_lock` (any fixed bigint; "mosroles" in ASCII).
_ROLE_CATALOG_LOCK = 0x6D6F73726F6C6573


def is_test_database(name: object) -> bool:
    """`mos_test`, `mos_iso` or an xdist worker copy `mos_test_gw<digits>`: exact names only."""
    return isinstance(name, str) and (
        name in {"mos_test", "mos_iso"} or WORKER_DB.fullmatch(name) is not None
    )


def _split(url: str) -> tuple[str, str] | None:
    found = _URL.match(url)
    return (found.group("base"), found.group("db")) if found else None


def _admin_libpq(url: str) -> str:
    """The same login on the `postgres` maintenance database (never the template itself)."""
    parts = _split(url)
    assert parts is not None
    return parts[0].replace("postgresql+psycopg://", "postgresql://", 1) + "/postgres"


def _drop(conn: psycopg.Connection[tuple[object, ...]], name: str) -> None:
    assert WORKER_DB.fullmatch(name), name
    conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def drop_worker_databases(url: str | None) -> None:
    """Controller side: drop every `mos_test_gw<digits>` next to the run's mos_test."""
    parts = _split(url) if url else None
    if url is None or parts is None or parts[1] != TEMPLATE:
        return
    with psycopg.connect(_admin_libpq(url), autocommit=True, connect_timeout=5) as conn:
        names = [
            str(row[0])
            for row in conn.execute("SELECT datname FROM pg_database ORDER BY 1").fetchall()
        ]
        for name in names:
            if WORKER_DB.fullmatch(name):
                _drop(conn, name)


def use_worker_database(worker_id: str | None) -> str | None:
    """Worker side: create `mos_test_<worker_id>` from mos_test and point DATABASE_URL at it.
    Returns the database name, or None when this is not an xdist worker on mos_test."""
    url = os.environ.get("DATABASE_URL")
    parts = _split(url) if url else None
    if not worker_id or url is None or parts is None or parts[1] != TEMPLATE:
        return None
    if not _WORKER_ID.fullmatch(worker_id):
        raise RuntimeError(f"unexpected xdist worker id {worker_id!r}")
    name = f"{TEMPLATE}_{worker_id}"
    with psycopg.connect(_admin_libpq(url), autocommit=True, connect_timeout=5) as conn:
        _drop(conn, name)
        create = sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
            sql.Identifier(name), sql.Identifier(TEMPLATE)
        )
        for attempt in range(20):
            try:
                conn.execute(create)
                break
            except psycopg.Error as error:
                if error.sqlstate != _IN_USE or attempt == 19:
                    raise
                time.sleep(0.25)
    os.environ["DATABASE_URL"] = f"{parts[0]}/{name}"
    return name


@contextmanager
def role_catalog_lock(url: str) -> Iterator[None]:
    """Serialise 0000_bootstrap.sql runs across xdist workers. Its ALTER ROLE and GRANT statements
    write cluster-wide catalogs (pg_db_role_setting, pg_auth_members), and two sessions updating
    one catalog row at once fail with "tuple concurrently updated". Advisory locks are scoped to
    a database, so the lock is held on `postgres`, the one database every worker shares."""
    with psycopg.connect(_admin_libpq(url), autocommit=True, connect_timeout=5) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (_ROLE_CATALOG_LOCK,))
        try:
            yield
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_ROLE_CATALOG_LOCK,))
