"""S11 T11 (AC 5): rows written inside a request carry its `X-Request-ID` in `job_runs`,
`agent_runs` and `validation_runs`; an explicit value wins; outside a request the column is
NULL. The `audit_log` leg is S10's: its rows go only through `audit()`."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from fastapi import FastAPI

from .conftest import ScratchDb
from .ops_support import TenantSeed, seed_tenant

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])


async def _write(seed: TenantSeed, *, explicit: str | None = None) -> dict[str, UUID]:
    """One `job_runs`, `agent_runs` and `validation_runs` row through the ORM."""
    from app.db.models.ops import JobRun
    from app.db.models.runs import AgentRun, ValidationRun
    from app.db.session import get_engine
    from sqlalchemy.ext.asyncio import AsyncSession

    extra: dict[str, Any] = {} if explicit is None else {"request_id": explicit}
    job = JobRun(
        id=uuid4(),
        tenant_id=seed.tenant_id,
        kind="bulk_import",
        entity_type="import",
        entity_id=uuid4(),
        payload_json={"import_id": str(uuid4())},
        correlation_id=uuid4(),
        **extra,
    )
    agent = AgentRun(
        id=uuid4(),
        tenant_id=seed.tenant_id,
        trigger="embed",
        status="running",
        agent_name="embedding",
        started_at=datetime.now(UTC),
        **extra,
    )
    validation = ValidationRun(
        id=uuid4(),
        tenant_id=seed.tenant_id,
        listing_id=seed.listing_id,
        marketplace="amazon",
        source="schema",
        passed=True,
        error_report_json={"errors": []},
        attempt=1,
        **extra,
    )
    async with AsyncSession(get_engine(), expire_on_commit=False) as session, session.begin():
        session.add_all([job, agent, validation])
    return {"job_runs": job.id, "agent_runs": agent.id, "validation_runs": validation.id}


def _stamps(db: ScratchDb, ids: dict[str, UUID]) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    with psycopg.connect(db.libpq, connect_timeout=5) as conn:
        for table, row_id in ids.items():
            row = conn.execute(
                f"SELECT request_id FROM {table} WHERE id = %s",  # noqa: S608 - fixed names
                (row_id,),
            ).fetchone()
            assert row is not None, table
            out[table] = row[0]
    return out


@pytest.fixture
def seed(head_db: ScratchDb) -> Iterator[TenantSeed]:
    """A committed tenant graph (the app's own engine must see it), deleted afterwards with
    every row the test wrote: `head_db` is shared by the whole session."""
    with psycopg.connect(head_db.libpq, autocommit=True, connect_timeout=5) as conn:
        side = seed_tenant(conn)
    yield side
    # agent_runs and validation_runs are append-only (0004): the database's own purge, as the
    # migrator, is the one path that removes a tenant and every row it wrote
    with psycopg.connect(head_db.libpq, autocommit=True, connect_timeout=5) as conn:
        conn.execute("SET SESSION AUTHORIZATION migrator")
        for mode in ("data", "final"):
            conn.execute("SELECT purge_tenant(%s, %s, force => true)", (side.tenant_id, mode))


@pytest.fixture
async def rows_app(
    head_db: ScratchDb, tmp_path: Any, monkeypatch: pytest.MonkeyPatch, seed: TenantSeed
) -> AsyncIterator[tuple[FastAPI, dict[str, dict[str, UUID]]]]:
    """`create_app()` on the head scratch database with test-only routes that write the rows."""
    from app.core.config import get_settings
    from app.db.session import dispose_engine
    from app.main import create_app

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("DATABASE_URL", head_db.url)
    monkeypatch.setenv("JWT_SECRET", "ci_only_not_a_real_secret_at_least_32_chars")
    get_settings.cache_clear()
    await dispose_engine()
    app = create_app()
    written: dict[str, dict[str, UUID]] = {}

    async def write_async(key: str) -> dict[str, bool]:
        written[key] = await _write(seed)
        return {"ok": True}

    async def write_explicit(key: str) -> dict[str, bool]:
        written[key] = await _write(seed, explicit="explicit-id")
        return {"ok": True}

    def write_sync(key: str) -> dict[str, bool]:
        # A sync route runs in the threadpool on a copy of the request's context; a sync
        # engine of its own, since the app's async engine belongs to the event loop.
        from app.db.models.ops import JobRun
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session
        from sqlalchemy.pool import NullPool

        engine = create_engine(head_db.url, poolclass=NullPool)
        job = JobRun(
            id=uuid4(),
            tenant_id=seed.tenant_id,
            kind="bulk_import",
            entity_type="import",
            entity_id=uuid4(),
            payload_json={},
            correlation_id=uuid4(),
        )
        try:
            with Session(engine, expire_on_commit=False) as session, session.begin():
                session.add(job)
        finally:
            engine.dispose()
        written[key] = {"job_runs": job.id}
        return {"ok": True}

    app.add_api_route("/__test/rows/{key}", write_async, methods=["POST"])
    app.add_api_route("/__test/rows-explicit/{key}", write_explicit, methods=["POST"])
    app.add_api_route("/__test/rows-sync/{key}", write_sync, methods=["POST"])
    yield app, written
    await dispose_engine()
    get_settings.cache_clear()


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_request_id_stamped_on_rows(
    rows_app: tuple[FastAPI, dict[str, dict[str, UUID]]], head_db: ScratchDb
) -> None:
    app, written = rows_app
    async with _client(app) as client:
        response = await client.post("/__test/rows/one", headers={"X-Request-ID": "abc"})
    assert response.status_code == 200
    assert _stamps(head_db, written["one"]) == {
        "job_runs": "abc",
        "agent_runs": "abc",
        "validation_runs": "abc",
    }


async def test_request_id_explicit_value_wins(
    rows_app: tuple[FastAPI, dict[str, dict[str, UUID]]], head_db: ScratchDb
) -> None:
    app, written = rows_app
    async with _client(app) as client:
        await client.post("/__test/rows-explicit/x", headers={"X-Request-ID": "abc"})
    assert set(_stamps(head_db, written["x"]).values()) == {"explicit-id"}


async def test_request_id_null_outside_request(
    rows_app: tuple[FastAPI, dict[str, dict[str, UUID]]], head_db: ScratchDb, seed: TenantSeed
) -> None:
    from sqlalchemy.exc import IntegrityError

    # agent_runs.request_id is NOT NULL in the DDL, so outside a request it cannot be written
    # without an explicit id; the nullable tables get NULL.
    with pytest.raises(IntegrityError):
        await _write(seed)
    ids = await _write_nullable_only(seed)
    assert _stamps(head_db, ids) == {"job_runs": None, "validation_runs": None}


async def _write_nullable_only(seed: TenantSeed) -> dict[str, UUID]:
    from app.db.models.ops import JobRun
    from app.db.models.runs import ValidationRun
    from app.db.session import get_engine
    from sqlalchemy.ext.asyncio import AsyncSession

    job = JobRun(
        id=uuid4(),
        tenant_id=seed.tenant_id,
        kind="bulk_import",
        entity_type="import",
        entity_id=uuid4(),
        payload_json={},
        correlation_id=uuid4(),
    )
    validation = ValidationRun(
        id=uuid4(),
        tenant_id=seed.tenant_id,
        listing_id=seed.listing_id,
        marketplace="amazon",
        source="schema",
        passed=True,
        error_report_json={"errors": []},
        attempt=1,
    )
    async with AsyncSession(get_engine(), expire_on_commit=False) as session, session.begin():
        session.add_all([job, validation])
    return {"job_runs": job.id, "validation_runs": validation.id}


async def test_request_id_sync_route_stamped(
    rows_app: tuple[FastAPI, dict[str, dict[str, UUID]]], head_db: ScratchDb
) -> None:
    app, written = rows_app
    async with _client(app) as client:
        response = await client.post("/__test/rows-sync/s", headers={"X-Request-ID": "sync-1"})
    assert response.status_code == 200
    assert set(_stamps(head_db, written["s"]).values()) == {"sync-1"}


async def test_request_id_concurrent_rows_isolated(
    rows_app: tuple[FastAPI, dict[str, dict[str, UUID]]], head_db: ScratchDb
) -> None:
    app, written = rows_app
    async with _client(app) as client:
        responses = await asyncio.gather(
            *(
                client.post(f"/__test/rows/c{n}", headers={"X-Request-ID": f"req-{n}"})
                for n in range(10)
            )
        )
    assert [r.status_code for r in responses] == [200] * 10
    for n in range(10):
        assert set(_stamps(head_db, written[f"c{n}"]).values()) == {f"req-{n}"}
