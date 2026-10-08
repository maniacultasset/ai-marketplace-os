"""S16 T7 (AC3, DW2; DATABASE.md:983-997): `AgentRunRepository` and `record_agent_run`, as app_rw."""

from __future__ import annotations

import uuid
from decimal import Decimal

import psycopg
import pytest
import sqlalchemy.exc

from .auth_support import World
from .brand_support import Conn, agent_rows, record
from .conftest import LOCAL_HOSTS

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)


async def test_record_inserts_one_row(products_world: World, auth_db: Conn) -> None:
    from app.services.telemetry import record_agent_run
    from mkos_schemas.agent import TokenUsage

    acme = products_world.tenant("acme")
    job = uuid.uuid4()
    rec = record(
        acme,
        job_id=job,
        attempt=2,
        tokens=TokenUsage(input=42),
        cost_usd=Decimal("0.000001"),
        duration_ms=15,
        input_digest="ab" * 32,
    )
    await record_agent_run(rec)
    (row,) = agent_rows(auth_db)
    assert row["id"] == rec.id and row["tenant_id"] == acme and row["job_id"] == job
    assert (row["agent_name"], row["trigger"], row["status"]) == ("embedding", "embed", "completed")
    assert (row["tokens_in"], row["tokens_out"], row["attempt"]) == (42, 0, 2)
    assert row["model_used"] == "text-embedding-3-small" and row["request_id"] == "req-s16-test"
    assert row["input_digest"] == "ab" * 32 and row["duration_ms"] == 15
    assert row["finished_at"] is not None and row["finished_at"] >= row["started_at"]


async def test_record_replay_same_id_no_second_row(products_world: World, auth_db: Conn) -> None:
    from app.services.telemetry import record_agent_run

    rec = record(products_world.tenant("acme"))
    await record_agent_run(rec)
    await record_agent_run(rec)
    assert len(agent_rows(auth_db)) == 1


async def test_record_cost_6dp_round_trip(products_world: World, auth_db: Conn) -> None:
    from app.services.telemetry import record_agent_run

    await record_agent_run(record(products_world.tenant("acme"), cost_usd=Decimal("1.234567")))
    (row,) = agent_rows(auth_db)
    assert row["cost_usd"] == Decimal("1.234567") and str(row["cost_usd"]) == "1.234567"


async def test_record_failed_requires_model(products_world: World, auth_db: Conn) -> None:
    from app.services.telemetry import record_agent_run

    with pytest.raises(sqlalchemy.exc.IntegrityError) as info:
        await record_agent_run(
            record(products_world.tenant("acme"), model_used=None, status="failed")
        )
    assert "ck_agent_runs_model_used" in str(info.value.orig.diag.constraint_name)  # type: ignore[union-attr]
    assert agent_rows(auth_db) == []


async def test_record_other_tenant_refused_before_sql(products_world: World, auth_db: Conn) -> None:
    from app.db.guard import CrossTenantQueryError
    from app.repositories.uow import tenant_uow

    acme, bharat = products_world.tenant("acme"), products_world.tenant("bharat")
    async with tenant_uow(acme) as uow:
        with pytest.raises(CrossTenantQueryError):
            await uow.agent_runs.insert(record(bharat))
    assert agent_rows(auth_db) == []


async def test_record_own_transaction(products_world: World, auth_db: Conn) -> None:
    from app.repositories.uow import tenant_uow
    from app.services.telemetry import record_agent_run

    acme = products_world.tenant("acme")

    class BoomError(Exception):
        pass

    with pytest.raises(BoomError):
        async with tenant_uow(acme):
            await record_agent_run(record(acme))
            raise BoomError
    assert len(agent_rows(auth_db, acme)) == 1


async def test_record_app_rw_cannot_update(products_world: World, auth_db: Conn) -> None:
    import os

    from app.services.telemetry import record_agent_run

    acme = products_world.tenant("acme")
    await record_agent_run(record(acme))
    url = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(url) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", [str(acme)])
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE agent_runs SET tokens_in = 1")


async def test_record_without_user_ok(products_world: World, auth_db: Conn) -> None:
    from app.repositories.uow import tenant_uow

    acme = products_world.tenant("acme")
    async with tenant_uow(acme) as uow:
        from sqlalchemy import text

        user = (
            await uow.session.execute(text("SELECT current_setting('app.user_id', true)"))
        ).scalar()
        await uow.agent_runs.insert(record(acme))
    assert user in (None, "")
    assert len(agent_rows(auth_db, acme)) == 1
