"""The `JobRun` model reads back the `queue` the BEFORE INSERT trigger `job_runs_set_queue()`
writes: right after a flush, with no refresh, whether or not the caller set a queue (the queue
helper reads `job.queue` to pick the procrastinate queue)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest
from app.db.models.ops import JobRun
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from .conftest import ScratchDb
from .ops_support import seed_tenant

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

LISTING_KINDS = {"listing_publish", "listing_status_sync"}
CASES = [
    ("listing_publish", None, "publish"),
    ("listing_publish", "default", "publish"),
    ("listing_status_sync", None, "publish"),
    ("listing_status_sync", "default", "publish"),
    ("bulk_import", None, "default"),
    ("bulk_import", "publish", "default"),
]


@pytest.fixture
def session(head_db: ScratchDb) -> Iterator[Session]:
    """A Session on one connection whose outer transaction is rolled back."""
    engine = create_engine(head_db.url, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                with Session(bind=connection) as session:
                    yield session
            finally:
                transaction.rollback()
    finally:
        engine.dispose()


@pytest.mark.parametrize(("kind", "sent", "expected"), CASES)
def test_job_run_queue_loaded_after_flush(
    session: Session, kind: str, sent: str | None, expected: str
) -> None:
    raw = session.connection().connection.driver_connection
    side = seed_tenant(raw)  # type: ignore[arg-type]
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
    job = JobRun(**columns)
    session.add(job)
    session.flush()
    # loaded by the INSERT itself, not by a lazy SELECT on first access
    assert "queue" in inspect(job).dict, "queue left unloaded after the flush"
    assert job.queue == expected
