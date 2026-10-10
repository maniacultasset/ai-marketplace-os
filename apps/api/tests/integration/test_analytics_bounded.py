"""S23 T6 (AC4; C12, C13): an analytics request reads in one READ ONLY transaction under one 5 s
budget, at most 2 per tenant at once, and its four reads use the 0009 indexes."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import psycopg
import pytest
from tests.timing import budget

from . import factories as f
from .analytics_support import FROM, TO, AnalyticsWorld, build_analytics_world
from .conftest import LOCAL_HOSTS, expect_sqlstate

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

Conn = psycopg.Connection[tuple[Any, ...]]


@pytest.fixture
def world(auth_db: Conn) -> AnalyticsWorld:
    return build_analytics_world(auth_db)


def _counter(conn: Conn, tenant: UUID) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT window_start, n FROM rate_limit_counters WHERE bucket_key = %s ORDER BY 1",
        [f"an:{tenant}"],
    ).fetchall()


@pytest.fixture
def slow_reads(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[float]]:
    """Every metric read becomes `SELECT pg_sleep(<seconds>)` (through the same error mapping);
    append the sleep to the yielded list to switch it on."""
    from app.repositories.analytics import AnalyticsRepository
    from sqlalchemy import func, select

    original = AnalyticsRepository._rows
    sleep: list[float] = []

    async def rows(self: AnalyticsRepository, stmt: Any) -> list[Any]:
        if sleep:
            await original(self, select(func.pg_sleep(sleep[0])))
            return []
        return await original(self, stmt)

    monkeypatch.setattr(AnalyticsRepository, "_rows", rows)
    yield sleep


async def test_analytics_read_only(world: AnalyticsWorld) -> None:
    from app.repositories.analytics import AnalyticsRepository
    from app.repositories.transactions import unit_of_work
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    acme = world.tenants["acme"]
    with pytest.raises(DBAPIError) as caught:
        async with unit_of_work(acme) as session:
            await AnalyticsRepository(session, acme).bounded()
            await session.execute(
                text("INSERT INTO rate_limit_counters VALUES ('ro-probe', now(), 1)")
            )
    assert getattr(caught.value.orig, "sqlstate", None) == "25006"


async def test_analytics_statement_timeout(world: AnalyticsWorld) -> None:
    from app.repositories.analytics import AnalyticsRepository
    from app.repositories.transactions import unit_of_work
    from sqlalchemy import text

    acme = world.tenants["acme"]
    async with unit_of_work(acme) as session:
        await AnalyticsRepository(session, acme).bounded()
        inside = [
            await session.scalar(text("SHOW statement_timeout")),
            await session.scalar(text("SHOW transaction_timeout")),
            await session.scalar(text("SHOW transaction_read_only")),
        ]
    async with unit_of_work(acme) as session:
        after = [
            await session.scalar(text("SHOW statement_timeout")),
            await session.scalar(text("SHOW transaction_timeout")),
            await session.scalar(text("SHOW transaction_read_only")),
        ]
    assert inside == ["5s", "5s", "on"]
    assert after[0] != "5s" and after[1] == "0" and after[2] == "off"


async def test_analytics_timeout_maps_to_budget_error(
    world: AnalyticsWorld, slow_reads: list[float], log_lines: Any
) -> None:
    """One statement past 5 s: statement_timeout cancels it; the error carries no SQL text."""
    from app.services.analytics import AnalyticsBudgetError, get_summary

    slow_reads.append(6)
    logs = log_lines()
    with pytest.raises(AnalyticsBudgetError) as caught:
        await get_summary(world.tenants["acme"], None, FROM, TO)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    text = str(caught.value) + logs.text()
    for leak in ("SELECT", "pg_sleep", "SQL:", "parameters"):
        assert leak not in text


async def test_analytics_request_budget(world: AnalyticsWorld, slow_reads: list[float]) -> None:
    """Four 2 s reads, each under statement_timeout: the transaction budget ends the request at
    about 5 s instead of letting it hold a pool connection for 8 s."""
    from app.services.analytics import AnalyticsBudgetError, get_summary

    slow_reads.append(2)
    started = time.monotonic()
    with pytest.raises(AnalyticsBudgetError):
        await get_summary(world.tenants["acme"], None, FROM, TO)
    elapsed = time.monotonic() - started
    assert 4.5 <= elapsed < budget(7.0), elapsed


async def test_analytics_request_budget_pool_recovers(
    world: AnalyticsWorld, slow_reads: list[float]
) -> None:
    from app.db.session import get_engine
    from app.services.analytics import AnalyticsBudgetError, get_summary

    acme = world.tenants["acme"]
    slow_reads.append(2)
    with pytest.raises(AnalyticsBudgetError):
        await get_summary(acme, None, FROM, TO)
    slow_reads.clear()
    body = await get_summary(acme, None, FROM, TO)
    assert body["listings_generated"] == 5
    assert get_engine().pool.checkedout() == 0  # type: ignore[attr-defined]


async def test_inflight_third_refused(world: AnalyticsWorld) -> None:
    from app.services.analytics import AnalyticsBusyError, inflight

    acme = world.tenants["acme"]
    async with inflight(acme), inflight(acme):
        with pytest.raises(AnalyticsBusyError):
            async with inflight(acme):
                pass
        assert [n for _w, n in _counter(world.conn, acme)] == [2]
    assert [n for _w, n in _counter(world.conn, acme)] == [0]


async def test_inflight_released_on_error(world: AnalyticsWorld, slow_reads: list[float]) -> None:
    from app.services.analytics import AnalyticsBudgetError, get_summary

    acme = world.tenants["acme"]
    slow_reads.append(6)
    with pytest.raises(AnalyticsBudgetError):
        await get_summary(acme, None, FROM, TO)
    assert [n for _w, n in _counter(world.conn, acme)] == [0]


async def test_inflight_other_tenant_independent(world: AnalyticsWorld) -> None:
    from app.services.analytics import inflight

    acme, bharat = world.tenants["acme"], world.tenants["bharat"]
    async with inflight(acme), inflight(acme), inflight(bharat), inflight(bharat):
        pass


async def test_inflight_straddles_minute_releases_own_row(
    world: AnalyticsWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import analytics

    acme = world.tenants["acme"]
    clock = [datetime(2026, 10, 9, 12, 0, 59, 900000, tzinfo=UTC)]
    monkeypatch.setattr(analytics, "utc_now", lambda: clock[0])
    async with analytics.inflight(acme):
        clock[0] = datetime(2026, 10, 9, 12, 1, 0, 100000, tzinfo=UTC)
        async with analytics.inflight(acme), analytics.inflight(acme):
            pass
    rows = _counter(world.conn, acme)
    assert [(w.minute, n) for w, n in rows] == [(0, 0), (1, 0)]


async def test_inflight_concurrent_requests_one_refused(world: AnalyticsWorld) -> None:
    """Three requests at once: exactly one is refused, and every slot comes back."""
    from app.services.analytics import AnalyticsBusyError, inflight

    acme = world.tenants["acme"]
    gate = asyncio.Event()

    async def one() -> str:
        try:
            async with inflight(acme):
                await gate.wait()
                return "ok"
        except AnalyticsBusyError:
            return "busy"

    tasks = [asyncio.create_task(one()) for _ in range(3)]
    await asyncio.sleep(0.5)
    gate.set()
    assert sorted(await asyncio.gather(*tasks)) == ["busy", "ok", "ok"]
    assert [n for _w, n in _counter(world.conn, acme)] == [0]


# ---- query plans over 50k rows per table for 2 tenants across 2 years ----

_SPREAD = (
    "timestamptz '2025-10-01' + mod(g, 730) * interval '1 day' + mod(g, 86400) * interval '1 s'"
)
_V_SPREAD = _SPREAD.replace("(g,", "(v.version - 1000,")


def _bulk(conn: Conn, tenants: list[UUID]) -> None:
    """25k rows per tenant in each of the four tables, as the admin login, triggers off."""
    for tenant in tenants:
        owner = f.member(conn, tenant)
        reviewer = f.member(conn, tenant, role="reviewer")
        account = f.marketplace_account(conn, tenant, connected_by_user_id=owner)
        listings = [f.listing(conn, tenant, marketplace_account_id=account) for _ in range(100)]
        listing = listings[0]
        version = f.version(conn, tenant, listing, generated_by_user_id=owner)
        template = conn.execute("SELECT id FROM category_templates LIMIT 1").fetchone()
        assert template is not None
        job = conn.execute(
            "INSERT INTO job_runs (tenant_id, kind, queue, entity_type, entity_id, payload_json, "
            "correlation_id) VALUES (%s, 'bulk_import', 'default', 'import', gen_random_uuid(), "
            "'{}', gen_random_uuid()) RETURNING id",
            [tenant],
        ).fetchone()
        assert job is not None
        conn.execute("SET session_replication_role = replica")
        try:
            conn.execute(
                "INSERT INTO listing_versions (tenant_id, listing_id, version, content_json, "  # noqa: S608 - fixed SQL, values bound
                "generated_by_user_id, origin, changed_paths, created_at) SELECT %s, "
                "(%s::uuid[])[1 + mod(g, 100)], 1000 + g, "
                "'{}', %s, CASE WHEN mod(g, 10) = 0 THEN 'edit' ELSE 'generation' END, "
                "CASE WHEN mod(g, 10) = 0 THEN '{content.title}'::text[] END, "
                f"{_SPREAD} FROM generate_series(1, 25000) g",
                [tenant, listings, owner],
            )
            conn.execute(
                "INSERT INTO review_decisions (tenant_id, listing_id, listing_version_id, "  # noqa: S608 - fixed SQL, values bound
                "decision, reason_code, feedback, decided_by_user_id, decided_at) "
                "SELECT %s, (%s::uuid[])[1 + mod(g, 100)], %s, 'reject', 'other', 'x', %s, "
                f"{_SPREAD} FROM generate_series(1, 25000) g",
                [tenant, listings, version, reviewer],
            )
            conn.execute(
                "INSERT INTO validation_runs (tenant_id, listing_id, marketplace, source, passed, "  # noqa: S608 - fixed SQL, values bound
                "error_report_json, attempt, rules_version, category_template_id, "
                "template_version, facts_digest, created_at) SELECT %s, (%s::uuid[])[1 + mod(g, 100)], "
                "'amazon', "
                "CASE WHEN mod(g, 3) = 0 THEN 'schema' ELSE 'rules_engine' END, true, '{}', "
                f"1 + mod(g, 2), 1, %s, 1, sha256(g::text::bytea), {_SPREAD} "
                "FROM generate_series(1, 25000) g",
                [tenant, listings, template[0]],
            )
            conn.execute(
                "INSERT INTO listing_submissions (tenant_id, listing_id, listing_version_id, "  # noqa: S608 - fixed SQL, values bound
                "marketplace, marketplace_account_id, job_id, attempt, operation, payload_sha256, "
                "payload_storage_key, facts_digest, idempotency_key, requested_by_user_id, "
                "actor_type, outcome, responded_at, resolved_at) SELECT %s, v.listing_id, v.id, "
                "'amazon', %s, %s, 1, 'create', sha256(v.version::text::bytea), "
                "%s || '/submissions/' || v.version || '.json', sha256(v.version::text::bytea), "
                f"md5(v.id::text || %s), %s, 'user', 'live', {_V_SPREAD}, {_V_SPREAD} "
                "FROM listing_versions v WHERE v.listing_id = ANY(%s) AND v.id <> %s",
                [tenant, account, job[0], str(tenant), str(tenant), owner, listings, version],
            )
        finally:
            conn.execute("RESET session_replication_role")


INDEX_OF = {
    "volume": "ix_listing_versions_tenant_id_created_at_generated",
    "decisions": "ix_review_decisions_tenant_id_decided_at",
    "validation": "ix_validation_runs_tenant_id_created_at_first_pass",
    "publish": "ix_listing_submissions_tenant_id_responded_at",
}


def _numbered(stmt: Any) -> tuple[str, list[Any]]:
    """The statement as `$n` SQL plus its parameters, for PREPARE."""
    from sqlalchemy.dialects.postgresql.psycopg import PGDialect_psycopg

    dialect = PGDialect_psycopg(paramstyle="numeric_dollar")  # type: ignore[no-untyped-call]
    compiled = stmt.compile(dialect=dialect)
    names = compiled.positiontup or []
    return str(compiled), [compiled.params[name] for name in names]


@pytest.fixture(scope="module")
def plan_db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:
    """A scratch database at head with the bulk rows, analysed (the plans need real stats)."""
    from .conftest import head_database

    with head_database(tmp_path_factory.mktemp("plans")) as db:
        with psycopg.connect(db.libpq, autocommit=True) as conn:
            from .ops_support import category_template

            category_template(conn)
            tenants = [f.tenant(conn), f.tenant(conn)]
            _bulk(conn, tenants)
            conn.execute("VACUUM ANALYZE")
        yield db, tenants


@pytest.mark.parametrize("days", [30, 366])
@pytest.mark.parametrize("metric", ["volume", "decisions", "validation", "publish"])
def test_analytics_query_plans(plan_db: Any, metric: str, days: int) -> None:
    """Each metric read names its 0009 index, also as a generic plan (the predicates' constants
    are SQL literals, so a prepared statement still matches the partial index)."""
    from datetime import timedelta

    from app.repositories.analytics import AnalyticsRepository

    db, tenants = plan_db
    repo = AnalyticsRepository(None, tenants[0])  # type: ignore[arg-type]
    hi = datetime(2027, 4, 1, tzinfo=UTC)
    lo = hi - timedelta(days=days)
    sql_text, params = _numbered(getattr(repo, f"{metric}_stmt")(lo, hi))
    with psycopg.connect(db.libpq, autocommit=True) as conn:
        conn.execute("SET plan_cache_mode = force_generic_plan")
        conn.execute(f"PREPARE q AS {sql_text}")
        marks = ", ".join(["%s"] * len(params))
        # EXECUTE takes literals: the client cursor interpolates them
        cursor = psycopg.ClientCursor(conn)
        plan = cursor.execute(f"EXPLAIN (FORMAT JSON) EXECUTE q({marks})", params).fetchone()
        conn.execute("DEALLOCATE q")
    assert plan is not None
    assert INDEX_OF[metric] in str(plan[0]), plan[0]


def test_analytics_read_only_sqlstate_helper(db: Conn) -> None:
    """The 25006 the read-only test relies on, at the SQL level."""
    db.execute("SET TRANSACTION READ ONLY")
    expect_sqlstate(db, "25006", "INSERT INTO rate_limit_counters VALUES ('x', now(), 1)")
