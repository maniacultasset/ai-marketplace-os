"""One test database per pytest-xdist worker (`make api-tests -n`).

`make api-tests` migrates `mos_test`, checks it and syncs the attribute dictionary; under xdist
each worker `gwN` then runs against its own copy, `mos_test_gwN`, made with
`CREATE DATABASE … TEMPLATE mos_test` (a file copy: schema, rows, grants and the dictionary rows
come with it; roles are cluster-wide). The controller drops any leftover copies before the
workers start and drops this run's copies at the end; a worker drops a stale copy of its own name
before it creates it. A serial run (no `-n`) and every other database are left alone.

Lane templates: several checkouts (git worktrees) can share one Postgres server, but only one of
them can own `mos_test`. A lane migrates its own template `mos_test_l<tag>` (tag
`[a-z0-9]{1,16}`, e.g. `mos_test_lsi4`) and points DATABASE_URL at it (`make api-tests
TEST_DATABASE_URL=…/mos_test_lsi4`); its workers then copy it to `mos_test_l<tag>_gwN`. The
template is whichever of `mos_test` or `mos_test_l<tag>` DATABASE_URL names, and a controller
drops only the worker copies of its own template: `mos_test_gwN` and other lanes' copies are
never touched.

`make mutation` (S26 T23) runs mutmut with several children, one pytest run per mutant in a
forked process. Each run holds one slot (an flock on `<slots dir>/<n>.lock`, freed when the
process exits) and uses that slot's copy, `mos_test_mut<n>`, made from mos_test when it does not
exist yet; `make mutation` drops the copies before and after the run, so each run starts from the
freshly migrated mos_test. Between mutants a copy is reused, as the serial run reused mos_test.
Slot copies exist for mos_test only: a lane template gets none.

Nothing here imports `app`: the worker's DATABASE_URL is pointed at its copy before any settings
or engine exist."""

from __future__ import annotations

import fcntl
import os
import re
import sys
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

import psycopg
from psycopg import sql

TEMPLATE = "mos_test"
_LANE_TAG = r"[a-z0-9]{1,16}"
# The databases xdist copies from: mos_test or a lane's `mos_test_l<tag>`.
TEMPLATE_DB = re.compile(rf"mos_test(?:_l{_LANE_TAG})?")
# A worker copy of any template (`mos_test_gwN`, `mos_test_l<tag>_gwN`); one template's own
# copies are `worker_copies(template)`.
WORKER_DB = re.compile(rf"mos_test(?:_l{_LANE_TAG})?_gw[0-9]+")
MUTANT_DB = re.compile(r"mos_test_mut[0-9]+")
MUTANT_SLOTS = 64
_WORKER_ID = re.compile(r"gw[0-9]+")
_URL = re.compile(r"^(?P<base>[a-z+]+://[^/]*)/(?P<db>[A-Za-z0-9_]+)$")
_IN_USE = "55006"  # object_in_use: the template had a session open for a moment
# pg_advisory_lock key for `role_catalog_lock` (any fixed bigint; "mosroles" in ASCII).
_ROLE_CATALOG_LOCK = 0x6D6F73726F6C6573


def is_test_database(name: object) -> bool:
    """`mos_test`, `mos_iso`, a lane template `mos_test_l<tag>`, an xdist worker copy
    (`mos_test_gw<digits>`, `mos_test_l<tag>_gw<digits>`) or a mutation slot copy
    `mos_test_mut<digits>`: exact names only."""
    return isinstance(name, str) and (
        name == "mos_iso"
        or TEMPLATE_DB.fullmatch(name) is not None
        or WORKER_DB.fullmatch(name) is not None
        or MUTANT_DB.fullmatch(name) is not None
    )


def worker_copies(template: str) -> re.Pattern[str]:
    """The worker copies of one template only: `<template>_gw<digits>`."""
    if TEMPLATE_DB.fullmatch(template) is None:
        raise ValueError(f"not a test template database: {template!r}")
    return re.compile(rf"{re.escape(template)}_gw[0-9]+")


def copies_to_drop(template: str, names: Iterable[str], pattern: re.Pattern[str]) -> list[str]:
    """What a controller on `template` drops from `names`: those `pattern` fully matches that
    are copies of `template` itself (its worker copies, or the slot copies when it is mos_test)."""
    own = worker_copies(template)
    return [
        name
        for name in names
        if pattern.fullmatch(name)
        and (
            own.fullmatch(name) is not None
            or (template == TEMPLATE and MUTANT_DB.fullmatch(name) is not None)
        )
    ]


def _split(url: str) -> tuple[str, str] | None:
    found = _URL.match(url)
    return (found.group("base"), found.group("db")) if found else None


def _template_of(url: str | None) -> tuple[str, str] | None:
    """(base, template) when the URL names mos_test or a lane template, else None."""
    parts = _split(url) if url else None
    if parts is None or TEMPLATE_DB.fullmatch(parts[1]) is None:
        return None
    return parts


def _admin_libpq(url: str) -> str:
    """The same login on the `postgres` maintenance database (never the template itself)."""
    parts = _split(url)
    assert parts is not None
    return parts[0].replace("postgresql+psycopg://", "postgresql://", 1) + "/postgres"


def _drop(conn: psycopg.Connection[tuple[object, ...]], name: str) -> None:
    assert WORKER_DB.fullmatch(name) or MUTANT_DB.fullmatch(name), name
    conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def drop_worker_databases(url: str | None, pattern: re.Pattern[str] | None = None) -> None:
    """Controller side: drop every worker copy of the run's template (`mos_test_gw<digits>`, or
    `mos_test_l<tag>_gw<digits>` on a lane template), or with MUTANT_DB every
    `mos_test_mut<digits>` next to mos_test. Any other template's copies are left alone."""
    found = _template_of(url)
    if url is None or found is None:
        return
    template = found[1]
    if pattern is MUTANT_DB and template != TEMPLATE:
        return
    with psycopg.connect(_admin_libpq(url), autocommit=True, connect_timeout=5) as conn:
        names = [
            str(row[0])
            for row in conn.execute("SELECT datname FROM pg_database ORDER BY 1").fetchall()
        ]
        for name in copies_to_drop(template, names, pattern or worker_copies(template)):
            _drop(conn, name)


def _create_copy(conn: psycopg.Connection[tuple[object, ...]], name: str, template: str) -> None:
    assert TEMPLATE_DB.fullmatch(template), template
    _drop(conn, name)
    create = sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
        sql.Identifier(name), sql.Identifier(template)
    )
    for attempt in range(20):
        try:
            conn.execute(create)
            return
        except psycopg.Error as error:
            if error.sqlstate != _IN_USE or attempt == 19:
                raise
            time.sleep(0.25)


def worker_database_name(template: str, worker_id: str) -> str:
    """`<template>_<worker_id>`, e.g. `mos_test_gw0` or `mos_test_lsi4_gw0`."""
    if not _WORKER_ID.fullmatch(worker_id):
        raise RuntimeError(f"unexpected xdist worker id {worker_id!r}")
    name = f"{template}_{worker_id}"
    if not worker_copies(template).fullmatch(name):
        raise RuntimeError(f"unexpected worker database name {name!r}")
    return name


def use_worker_database(worker_id: str | None) -> str | None:
    """Worker side: create `<template>_<worker_id>` from the template DATABASE_URL names
    (mos_test or a lane's `mos_test_l<tag>`) and point DATABASE_URL at it. Returns the database
    name, or None when this is not an xdist worker on a template."""
    url = os.environ.get("DATABASE_URL")
    parts = _template_of(url)
    if not worker_id or url is None or parts is None:
        return None
    template = parts[1]
    name = worker_database_name(template, worker_id)
    with psycopg.connect(_admin_libpq(url), autocommit=True, connect_timeout=5) as conn:
        _create_copy(conn, name, template)
    os.environ["DATABASE_URL"] = f"{parts[0]}/{name}"
    return name


# The slot this process holds: (pid, slot, open lock file). A forked child inherits the
# parent's entry, so the pid tells it to take its own slot.
_held_slot: tuple[int, int, IO[str]] | None = None


def _take_slot(slots_dir: Path) -> int:
    global _held_slot
    if _held_slot is not None and _held_slot[0] == os.getpid():
        return _held_slot[1]
    slots_dir.mkdir(parents=True, exist_ok=True)
    for slot in range(MUTANT_SLOTS):
        handle = (slots_dir / f"{slot}.lock").open("a")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            continue
        _held_slot = (os.getpid(), slot, handle)
        return slot
    raise RuntimeError(f"all {MUTANT_SLOTS} mutation database slots are in use in {slots_dir}")


def use_mutant_database(slots_dir: Path) -> str | None:
    """A mutmut child (MUTANT_UNDER_TEST set): take a free slot and point DATABASE_URL at its
    copy of mos_test. Returns the database name, or None off mos_test (and its slot copies)."""
    url = os.environ.get("DATABASE_URL")
    parts = _split(url) if url else None
    if url is None or parts is None:
        return None
    if parts[1] != TEMPLATE and MUTANT_DB.fullmatch(parts[1]) is None:
        return None
    slot = _take_slot(slots_dir)
    name = f"{TEMPLATE}_mut{slot}"
    template_url = f"{parts[0]}/{TEMPLATE}"
    with psycopg.connect(_admin_libpq(template_url), autocommit=True, connect_timeout=5) as conn:
        found = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
        if found is None:
            _create_copy(conn, name, TEMPLATE)
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


def main(argv: list[str]) -> int:
    """`python tests/support/xdist_db.py drop-mutant-databases`: `make mutation` runs it before
    and after mutmut (DATABASE_URL names the run's mos_test)."""
    if argv != ["drop-mutant-databases"]:
        sys.stderr.write("usage: xdist_db.py drop-mutant-databases\n")
        return 2
    drop_worker_databases(os.environ.get("DATABASE_URL"), MUTANT_DB)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
