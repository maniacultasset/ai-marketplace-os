"""`validation_runs`, `agent_runs` and `performance` (DATABASE.md §5.3 validation_runs, §5.4
agent_runs, §5.5 performance; A§19, T60, T89, D7): the 7-value source, attempt bounds, the
rules-engine pins, the agent-run CHECKs and cost type, the performance key and tenant FK, and the
soft references that must not be FKs."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg.types.json import Jsonb

from . import factories as f
from .conftest import expect_sqlstate

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]
DIGEST = b"\x05" * 32
TEMPLATE = "00000000-0000-0000-0000-00000000000b"
ERROR_SOURCES = [
    "schema",
    "rules_engine",
    "vision_model",
    "ai_model",
    "amazon_adapter",
    "flipkart_adapter",
    "system",
]
# Soft references (DATABASE.md §1): job ids, template pins (FKs arrive in 0003), and the
# references from a longer-lived row to one pruned sooner.
SOFT_REFERENCE_COLUMNS = frozenset(
    {
        "job_id",
        "regeneration_job_id",
        "import_id",
        "category_template_id",
        "template_version",
        "validation_run_id",
        "last_validation_run_id",
        "human_task_id",
        "recurrence_of_id",
        "error_map_id",
        "source_id",
    }
)


def _listing(db: Conn) -> tuple[UUID, UUID]:
    tenant_id = f.tenant(db)
    return tenant_id, f.listing(db, tenant_id)


# ---- validation_runs ----


@pytest.mark.parametrize("source", ERROR_SOURCES)
def test_vocab_checks_validation_source(db: Conn, source: str) -> None:
    tenant_id, listing_id = _listing(db)
    pins: dict[str, Any] = {}
    if source == "rules_engine":
        pins = {
            "rules_version": 7,
            "category_template_id": TEMPLATE,
            "template_version": 1,
            "facts_digest": DIGEST,
        }
    f.validation_run(db, tenant_id, listing_id, source=source, **pins)


@pytest.mark.parametrize("source", ["rules", "api", "vision", "Schema"])
def test_vocab_checks_validation_source_rejects(db: Conn, source: str) -> None:
    tenant_id, listing_id = _listing(db)
    f.expect_violation(
        db,
        "23514",
        "ck_validation_runs_source",
        lambda: f.validation_run(db, tenant_id, listing_id, source=source),
    )


@pytest.mark.parametrize(("attempt", "ok"), [(0, False), (1, True), (4, True), (5, False)])
def test_validation_attempt_bounds(db: Conn, attempt: int, ok: bool) -> None:
    tenant_id, listing_id = _listing(db)
    if ok:
        f.validation_run(db, tenant_id, listing_id, attempt=attempt)
    else:
        f.expect_violation(
            db,
            "23514",
            "ck_validation_runs_attempt",
            lambda: f.validation_run(db, tenant_id, listing_id, attempt=attempt),
        )


VR_REJECTS: dict[str, tuple[dict[str, Any], str]] = {
    "passed with a blocking error": ({"blocking_count": 1}, "ck_validation_runs_passed"),
    "failed without a blocking error": ({"passed": False}, "ck_validation_runs_passed"),
    "negative warnings": ({"warning_count": -1}, "ck_validation_runs_counts"),
    "rules_engine without pins": ({"source": "rules_engine"}, "vr_rules_pins"),
    "rules_engine without digest": (
        {
            "source": "rules_engine",
            "rules_version": 7,
            "category_template_id": TEMPLATE,
            "template_version": 1,
        },
        "vr_rules_pins",
    ),
    "template id without version": (
        {"category_template_id": TEMPLATE},
        "ck_validation_runs_template_pin",
    ),
    "facts_digest 31": ({"facts_digest": b"\x05" * 31}, "ck_validation_runs_facts_digest"),
    "duration": ({"duration_ms": -1}, "ck_validation_runs_duration_ms"),
    "report not an object": (
        {"error_report_json": Jsonb([])},
        "ck_validation_runs_error_report_json",
    ),
    "marketplace": ({"marketplace": "nykaa"}, "ck_validation_runs_marketplace"),
}


@pytest.mark.parametrize("case", sorted(VR_REJECTS))
def test_checks_reject_validation_runs(db: Conn, case: str) -> None:
    columns, constraint = VR_REJECTS[case]
    tenant_id, listing_id = _listing(db)
    f.expect_violation(
        db, "23514", constraint, lambda: f.validation_run(db, tenant_id, listing_id, **columns)
    )


def test_validation_version_same_listing(db: Conn) -> None:
    tenant_id, listing_id = _listing(db)
    other = f.listing(db, tenant_id)
    f.expect_violation(
        db,
        "23503",
        "fk_validation_runs_listing_version_id",
        lambda: f.validation_run(
            db, tenant_id, listing_id, listing_version_id=f.version(db, tenant_id, other)
        ),
    )
    version_id = f.version(db, tenant_id, listing_id)
    f.validation_run(
        db,
        tenant_id,
        listing_id,
        listing_version_id=version_id,
        passed=False,
        blocking_count=2,
        warning_count=1,
    )


# ---- agent_runs ----

AR_REJECTS: dict[str, tuple[dict[str, Any], str]] = {
    "trigger": ({"trigger": "regenerate"}, "ck_agent_runs_trigger"),
    "status": ({"status": "succeeded"}, "ck_agent_runs_status"),
    "model": ({"model_used": None}, "ck_agent_runs_model_used"),
    "agent_name": ({"agent_name": "writer"}, "ck_agent_runs_agent_name"),
    "cost negative": ({"cost_usd": -1}, "ck_agent_runs_cost_usd"),
    "cost NaN": ({"cost_usd": "NaN"}, "ck_agent_runs_cost_usd"),
    "tokens negative": ({"tokens_out": -1}, "ck_agent_runs_tokens"),
    "duration negative": ({"duration_ms": -1}, "ck_agent_runs_duration_ms"),
    "attempt 4": ({"attempt": 4}, "ck_agent_runs_attempt"),
    "finished before started": (
        {"finished_at": "2026-10-04T00:00:00Z"},
        "ck_agent_runs_finished_at",
    ),
    "report not an object": ({"error_report_json": Jsonb("x")}, "ck_agent_runs_error_report_json"),
    "version without listing": (
        {"listing_version_id": "00000000-0000-0000-0000-00000000000c"},
        "ck_agent_runs_listing_version",
    ),
}


@pytest.mark.parametrize("case", sorted(AR_REJECTS))
def test_checks_reject_agent_runs(db: Conn, case: str) -> None:
    columns, constraint = AR_REJECTS[case]
    tenant_id = f.tenant(db)
    f.expect_violation(db, "23514", constraint, lambda: f.agent_run(db, tenant_id, **columns))


def test_checks_accept_agent_runs(db: Conn) -> None:
    tenant_id, listing_id = _listing(db)
    version_id = f.version(db, tenant_id, listing_id)
    for trigger, agent in (
        ("generate", "content"),
        ("correct", "correction"),
        ("embed", "embedding"),
        ("vision", "image"),
    ):
        f.agent_run(db, tenant_id, trigger=trigger, agent_name=agent)
    f.agent_run(db, tenant_id, status="running", model_used=None)
    f.agent_run(
        db,
        tenant_id,
        product_id=f.product(db, tenant_id),
        listing_id=listing_id,
        listing_version_id=version_id,
        cost_usd="0.000413",
        tokens_in=10,
        tokens_out=20,
        tokens_cached_in=5,
        attempt=3,
        finished_at="2026-10-05T00:00:01Z",
        status="failed",
        error_report_json={"errors": []},
        job_id="00000000-0000-0000-0000-00000000000d",
    )
    row = db.execute(
        "SELECT max(cost_usd)::text FROM agent_runs WHERE tenant_id = %s", (tenant_id,)
    )
    assert row.fetchone() == ("0.000413",)


def test_agent_run_version_same_listing(db: Conn) -> None:
    tenant_id, listing_id = _listing(db)
    other_version = f.version(db, tenant_id, f.listing(db, tenant_id))
    f.expect_violation(
        db,
        "23503",
        "fk_agent_runs_listing_version_id",
        lambda: f.agent_run(db, tenant_id, listing_id=listing_id, listing_version_id=other_version),
    )


def test_agent_runs_cost_usd_type(db: Conn) -> None:
    row = db.execute(
        "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
        "WHERE attrelid = 'agent_runs'::regclass AND attname = 'cost_usd'"
    ).fetchone()
    assert row == ("numeric(12,6)",)


def test_agent_runs_upsert_target(db: Conn) -> None:
    """Inserts use `ON CONFLICT (id) DO NOTHING` (DATABASE.md §5.4): a replay is a no-op."""
    tenant_id = f.tenant(db)
    run_id = f.agent_run(db, tenant_id)
    db.execute(
        "INSERT INTO agent_runs (id, tenant_id, trigger, status, model_used, request_id, "
        "agent_name, started_at) VALUES (%s, %s, 'generate', 'completed', 'm', 'r', 'content', "
        "now()) ON CONFLICT (id) DO NOTHING",
        (run_id, tenant_id),
    )
    assert db.execute("SELECT count(*) FROM agent_runs WHERE id = %s", (run_id,)).fetchone() == (1,)


# ---- performance ----


def test_performance_listing_date_key(db: Conn) -> None:
    tenant_id, listing_id = _listing(db)
    f.performance(db, tenant_id, listing_id)
    f.expect_violation(
        db,
        "23505",
        "performance_listing_date_key",
        lambda: f.performance(db, tenant_id, listing_id),
    )
    f.performance(db, tenant_id, listing_id, date="2026-10-02")
    db.execute(
        "INSERT INTO performance (tenant_id, listing_id, marketplace, date, clicks) "
        "VALUES (%s, %s, 'amazon', '2026-10-01', 5) "
        "ON CONFLICT (listing_id, date) DO UPDATE SET clicks = EXCLUDED.clicks",
        (tenant_id, listing_id),
    )


def test_performance_marketplace_must_match_listing(db: Conn) -> None:
    tenant_id, listing_id = _listing(db)
    f.expect_violation(
        db,
        "23503",
        "fk_performance_listing_id_marketplace",
        lambda: f.performance(db, tenant_id, listing_id, marketplace="flipkart"),
    )


def test_performance_listing_cross_tenant_rejected(db: Conn) -> None:
    """D7: a tenant-A row pointing at tenant B's listing fails the `(tenant_id, listing_id)`
    FK, which DATABASE.md's `→ l/mkt` alone does not catch."""
    tenant_a, _ = _listing(db)
    _, listing_b = _listing(db)
    f.expect_violation(
        db,
        "23503",
        "fk_performance_listing_id",
        lambda: f.performance(db, tenant_a, listing_b),
    )


PERF_REJECTS: dict[str, tuple[dict[str, Any], str]] = {
    "negative impressions": ({"impressions": -1}, "ck_performance_counts"),
    "negative returns": ({"returns": -1}, "ck_performance_counts"),
    "ranking 0": ({"ranking_position": 0}, "ck_performance_ranking_position"),
    "revenue NaN": ({"revenue": "NaN"}, "ck_performance_revenue"),
    "revenue negative": ({"revenue": -1}, "ck_performance_revenue"),
    "currency": ({"currency": "USD"}, "ck_performance_currency"),
    "metrics array": ({"metrics": Jsonb([])}, "ck_performance_metrics"),
    "marketplace": ({"marketplace": "x"}, "ck_performance_marketplace"),
}


@pytest.mark.parametrize("case", sorted(PERF_REJECTS))
def test_checks_reject_performance(db: Conn, case: str) -> None:
    columns, constraint = PERF_REJECTS[case]
    tenant_id, listing_id = _listing(db)
    f.expect_violation(
        db, "23514", constraint, lambda: f.performance(db, tenant_id, listing_id, **columns)
    )


def test_performance_defaults(db: Conn) -> None:
    tenant_id, listing_id = _listing(db)
    row_id = f.performance(db, tenant_id, listing_id, ranking_position=1, revenue="10.50")
    row = db.execute(
        "SELECT impressions, clicks, orders, returns, currency, metrics FROM performance "
        "WHERE id = %s",
        (row_id,),
    ).fetchone()
    assert row == (0, 0, 0, 0, "INR", {})


# ---- soft references ----


def test_template_pin_and_job_id_have_no_fk(db: Conn) -> None:
    """No FK in the schema covers a soft-reference column (DATABASE.md §1: job references,
    template pins until 0003, and longer-lived rows pointing at rows pruned sooner)."""
    rows = db.execute(
        "SELECT con.conname, a.attname FROM pg_constraint con "
        "JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = ANY (con.conkey) "
        "WHERE con.contype = 'f' AND con.connamespace = 'public'::regnamespace"
    ).fetchall()
    assert [r for r in rows if r[1] in SOFT_REFERENCE_COLUMNS] == []
    present = db.execute(
        "SELECT count(*) FROM pg_attribute WHERE attrelid IN ('listing_versions'::regclass, "
        "'validation_runs'::regclass, 'agent_runs'::regclass) "
        "AND attname IN ('job_id', 'category_template_id', 'template_version')"
    ).fetchone()
    assert present == (5,)
    expect_sqlstate(
        db,
        "42P01",
        "SELECT 1 FROM job_runs",  # 0003: until then nothing could reference it
    )


# ---- cross-tenant FKs ----

Build = Callable[[Conn, UUID, UUID], None]


def _vr_listing(db: Conn, a: UUID, b: UUID) -> None:
    f.validation_run(db, a, f.listing(db, b))


def _ar_product(db: Conn, a: UUID, b: UUID) -> None:
    f.agent_run(db, a, product_id=f.product(db, b))


def _ar_listing(db: Conn, a: UUID, b: UUID) -> None:
    f.agent_run(db, a, listing_id=f.listing(db, b))


def _perf_listing(db: Conn, a: UUID, b: UUID) -> None:
    f.performance(db, a, f.listing(db, b))


CROSS_TENANT: dict[str, tuple[Build, str]] = {
    "validation_runs.listing_id": (_vr_listing, "fk_validation_runs_listing_id"),
    "agent_runs.product_id": (_ar_product, "fk_agent_runs_product_id"),
    "agent_runs.listing_id": (_ar_listing, "fk_agent_runs_listing_id"),
    "performance.listing_id": (_perf_listing, "fk_performance_listing_id"),
}
CROSS_TENANT_FK_COLUMNS = frozenset(CROSS_TENANT)


@pytest.mark.parametrize("column", sorted(CROSS_TENANT))
def test_0002_cross_tenant_fk_runs(db: Conn, column: str) -> None:
    build, constraint = CROSS_TENANT[column]
    tenant_a, tenant_b = f.tenant(db), f.tenant(db)
    f.expect_violation(db, "23503", constraint, lambda: build(db, tenant_a, tenant_b))
