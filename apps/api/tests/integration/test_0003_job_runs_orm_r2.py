"""Round-2 attacks on the `JobRun.queue` fix (FetchedValue + eager_defaults + the
`_drop_caller_queue` before_insert listener): a multi-row flush that mixes kinds and caller
queues (insertmanyvalues with RETURNING), `Session.merge` of a new object, a clean attribute
history after the flush, and the app's real path, an `AsyncSession` on psycopg async, where a lazy
load of `queue` after the flush would raise instead of reading the trigger's value."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from app.db.models.ops import JobRun
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from .conftest import ScratchDb, head_database
from .ops_support import TenantSeed, seed_tenant

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

LISTING_KINDS = {"listing_publish", "listing_status_sync"}
# (kind, queue the caller sends or None, queue the trigger writes)
MIXED = [
    ("bulk_import", "publish", "default"),
    ("listing_publish", None, "publish"),
    ("bulk_import", None, "default"),
    ("listing_status_sync", "default", "publish"),
    ("bulk_import", "publish", "default"),
    ("bulk_import", "default", "default"),
]


@pytest.fixture(scope="module")
def orm_db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[ScratchDb, TenantSeed]]:
    """A private head database with one committed tenant graph (the async session cannot drive
    the sync row factories, so the seed is committed up front; the database is dropped after)."""
    with head_database(tmp_path_factory.mktemp("orm_r2")) as db:
        with psycopg.connect(db.libpq, connect_timeout=5) as conn:
            side = seed_tenant(conn)
            conn.commit()
        yield db, side


def job(side: TenantSeed, kind: str, sent: str | None) -> JobRun:
    columns: dict[str, Any] = {
        "tenant_id": side.tenant_id,
        "kind": kind,
        "entity_type": "import",
        "entity_id": uuid4(),
        "payload_json": {},
        "correlation_id": uuid4(),
        "created_by": side.user_id,
    }
    if kind in LISTING_KINDS:
        columns.update(listing_id=side.listing_id, entity_type="listing")
    if sent is not None:
        columns["queue"] = sent
    return JobRun(**columns)


@pytest.fixture
def session(orm_db: tuple[ScratchDb, TenantSeed]) -> Iterator[Session]:
    engine = create_engine(orm_db[0].url, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                with Session(bind=connection) as s:
                    yield s
            finally:
                transaction.rollback()
    finally:
        engine.dispose()


def db_queues(session: Session, ids: list[UUID]) -> dict[UUID, str]:
    rows = session.execute(
        select(JobRun.__table__.c.id, JobRun.__table__.c.queue).where(
            JobRun.__table__.c.id.in_(ids)
        )
    )
    return {row.id: row.queue for row in rows}


def test_multi_row_flush_mixed_kinds(
    session: Session, orm_db: tuple[ScratchDb, TenantSeed]
) -> None:
    jobs = [job(orm_db[1], kind, sent) for kind, sent, _ in MIXED]
    session.add_all(jobs)
    session.flush()
    for row, (_kind, _sent, expected) in zip(jobs, MIXED, strict=True):
        assert "queue" in inspect(row).dict
        assert row.queue == expected
    assert db_queues(session, [row.id for row in jobs]) == {
        row.id: expected for row, (_k, _s, expected) in zip(jobs, MIXED, strict=True)
    }


def test_history_clean_after_flush_and_next_flush_leaves_queue(
    session: Session, orm_db: tuple[ScratchDb, TenantSeed]
) -> None:
    row = job(orm_db[1], "listing_publish", "default")
    session.add(row)
    session.flush()
    assert not inspect(row).attrs.queue.history.has_changes()
    assert row not in session.dirty
    row.request_id = "s8-r2"  # an unrelated UPDATE must not carry the caller's stale queue
    session.flush()
    assert db_queues(session, [row.id]) == {row.id: "publish"}
    assert row.queue == "publish"


def test_merge_of_new_object_loads_trigger_queue(
    session: Session, orm_db: tuple[ScratchDb, TenantSeed]
) -> None:
    merged = session.merge(job(orm_db[1], "bulk_import", "publish"))
    session.flush()
    assert "queue" in inspect(merged).dict
    assert merged.queue == "default"


@pytest.mark.parametrize(("kind", "sent", "expected"), MIXED)
async def test_async_session_reads_queue_without_lazy_load(
    orm_db: tuple[ScratchDb, TenantSeed], kind: str, sent: str | None, expected: str
) -> None:
    engine = create_async_engine(orm_db[0].url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                async with AsyncSession(bind=connection) as s:
                    row = job(orm_db[1], kind, sent)
                    s.add(row)
                    await s.flush()
                    # a lazy load here would raise MissingGreenlet; the INSERT must load it
                    assert "queue" in inspect(row).dict
                    assert row.queue == expected
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()
