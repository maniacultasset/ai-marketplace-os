"""0004/05_last_owner (DATABASE.md §5.1 tenant_memberships :256): a deferred constraint trigger keeps
an active owner on every tenant that is not offboarded; an advisory lock serialises the count. The
trigger fires at COMMIT, so these tests commit, on a module-scoped scratch database at head."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import psycopg
import pytest

from . import factories as f
from .conftest import ScratchDb, head_database

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]

DEMOTE = "UPDATE tenant_memberships SET role = 'operator' WHERE tenant_id = %s AND user_id = %s"
DEACTIVATE = (
    "UPDATE tenant_memberships SET is_active = false, deactivated_at = now() "
    "WHERE tenant_id = %s AND user_id = %s"
)


def _as_app_rw(conn: Conn, tenant: UUID) -> None:
    """The app's unit of work: the tenant GUC (0005's RLS), then the app role."""
    conn.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant)])
    conn.execute("SET ROLE app_rw")


@pytest.fixture(scope="module")
def owners_db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ScratchDb]:
    with head_database(tmp_path_factory.mktemp("last_owner")) as scratch:
        yield scratch


def _tenant(db: ScratchDb, owners: int = 1, **columns: Any) -> tuple[UUID, list[UUID]]:
    with psycopg.connect(db.libpq, autocommit=True) as conn:
        tenant = f.tenant(conn, **columns)
        return tenant, [f.member(conn, tenant, role="owner") for _ in range(owners)]


def _commit(db: ScratchDb, *statements: tuple[str, tuple[Any, ...]]) -> str | None:
    """Run the statements as `app_rw` in one transaction of their tenant (each statement's first
    parameter) and commit; the SQLSTATE if it fails."""
    with psycopg.connect(db.libpq) as conn:
        _as_app_rw(conn, statements[0][1][0])
        try:
            for query, params in statements:
                conn.execute(query, params)
            conn.commit()
        except psycopg.Error as error:
            conn.rollback()
            return error.sqlstate
    return None


def _active_owners(db: ScratchDb, tenant: UUID) -> int:
    with psycopg.connect(db.libpq, autocommit=True) as conn:
        row = conn.execute(
            "SELECT count(*) FROM tenant_memberships "
            "WHERE tenant_id = %s AND role = 'owner' AND is_active",
            (tenant,),
        ).fetchone()
    assert row is not None
    return int(row[0])


def test_last_owner_guard_demote(owners_db: ScratchDb) -> None:
    tenant, (owner,) = _tenant(owners_db)
    assert _commit(owners_db, (DEMOTE, (tenant, owner))) == "OWN01"
    assert _active_owners(owners_db, tenant) == 1


def test_last_owner_guard_deactivate(owners_db: ScratchDb) -> None:
    tenant, (owner,) = _tenant(owners_db)
    assert _commit(owners_db, (DEACTIVATE, (tenant, owner))) == "OWN01"
    assert _active_owners(owners_db, tenant) == 1


def test_last_owner_guard_second_owner_allows(owners_db: ScratchDb) -> None:
    tenant, (first, _) = _tenant(owners_db, owners=2)
    assert _commit(owners_db, (DEMOTE, (tenant, first))) is None
    assert _active_owners(owners_db, tenant) == 1


def test_last_owner_guard_swap_in_one_transaction(owners_db: ScratchDb) -> None:
    """Deferred: demoting the owner before promoting the successor is fine at commit."""
    tenant, (owner,) = _tenant(owners_db)
    with psycopg.connect(owners_db.libpq, autocommit=True) as conn:
        successor = f.member(conn, tenant, role="operator")
    promote = "UPDATE tenant_memberships SET role = 'owner' WHERE tenant_id = %s AND user_id = %s"
    assert _commit(owners_db, (DEMOTE, (tenant, owner)), (promote, (tenant, successor))) is None
    assert _active_owners(owners_db, tenant) == 1


def test_last_owner_guard_non_owner_change_unchecked(owners_db: ScratchDb) -> None:
    """A tenant with no owner at all (staff set-up) can still update its other members."""
    with psycopg.connect(owners_db.libpq, autocommit=True) as conn:
        tenant = f.tenant(conn)
        operator = f.member(conn, tenant, role="operator")
    assert _commit(owners_db, (DEACTIVATE, (tenant, operator))) is None


def test_last_owner_guard_race_two_owners(owners_db: ScratchDb) -> None:
    tenant, (first, second) = _tenant(owners_db, owners=2)
    outcomes: list[str | None] = []
    barrier = threading.Barrier(2)

    def demote(user: UUID) -> None:
        with psycopg.connect(owners_db.libpq) as conn:
            _as_app_rw(conn, tenant)
            try:
                conn.execute(DEMOTE, (tenant, user))
                barrier.wait(timeout=5)
                conn.commit()
                outcomes.append(None)
            except psycopg.Error as error:
                conn.rollback()
                outcomes.append(error.sqlstate)

    threads = [threading.Thread(target=demote, args=(user,)) for user in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    assert sorted(outcomes, key=str) == sorted([None, "OWN01"], key=str)
    assert _active_owners(owners_db, tenant) == 1


def test_last_owner_guard_immediate_mode_raises_at_update(owners_db: ScratchDb) -> None:
    tenant, (owner,) = _tenant(owners_db)
    with psycopg.connect(owners_db.libpq) as conn:
        _as_app_rw(conn, tenant)
        conn.execute("SET CONSTRAINTS last_owner_guard IMMEDIATE")
        with pytest.raises(psycopg.Error) as raised:
            conn.execute(DEMOTE, (tenant, owner))
        assert raised.value.sqlstate == "OWN01"


def test_last_owner_guard_offboarded_exempt(owners_db: ScratchDb) -> None:
    tenant, (owner,) = _tenant(owners_db, status="offboarded", offboarded_at="2026-10-01T00:00:00Z")
    assert _commit(owners_db, (DEACTIVATE, (tenant, owner))) is None
