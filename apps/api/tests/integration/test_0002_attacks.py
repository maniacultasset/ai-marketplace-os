"""Attacks on revision 0002/0002b by the step's tester: the documented purge order over a full
0002 graph, writes as `app_rw` (sequence included), ON CONFLICT targets for the later upserts,
cross-tenant vector search on the exact path, numeric and vector edge values, and the 0002b
gate refusing an upgrade end to end."""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from app.db import migrations
from app.db.migrations import ExtensionTooOld
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from psycopg import sql

from . import factories as f
from . import test_0002_catalog as catalog
from . import test_0002_rules as rules
from .conftest import ScratchDb, expect_sqlstate

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]
MODEL = "text-embedding-3-small"

TABLES_0002 = (
    "rules",
    "marketplace_accounts",
    "images",
    "batches",
    "listings",
    "listing_versions",
    "review_decisions",
    "listing_items",
    "validation_runs",
    "agent_runs",
    "performance",
    "brand_documents",
    "brand_chunks",
    "human_tasks",
    "listing_issues",
)

# DATABASE.md §9 purge_tenant(t, 'data'): children first, version pointers deferred.
PURGE_ORDER = (
    "listing_issues",
    "human_tasks",
    "listing_items",
    "review_decisions",
    "batches",
    "validation_runs",
    "agent_runs",
    "performance",
    "listing_versions",
    "listings",
    "marketplace_accounts",
    "brand_chunks",
    "brand_documents",
    "rules",
    "images",
)


def _graph(db: Conn, tenant_id: UUID) -> None:
    """One row (at least) in every 0002 tenant table, wired through every pointer kind."""
    owner = f.member(db, tenant_id)
    reviewer = f.member(db, tenant_id, role="reviewer")
    style = f.product(db, tenant_id, "style")
    colourway = f.colourway(db, tenant_id, style)
    image_id = f.image(db, tenant_id, style, colourway_id=colourway, slot="main")
    account = f.marketplace_account(db, tenant_id, connected_by_user_id=owner)
    batch_id = f.batch(db, tenant_id, created_by_user_id=owner, kind="publish")
    listing_id = f.listing(db, tenant_id, style, marketplace_account_id=account)
    version_id = f.version(db, tenant_id, listing_id, generated_by_user_id=owner)
    db.execute(
        "UPDATE listings SET current_version_id = %s, approved_version_id = %s WHERE id = %s",
        (version_id, version_id, listing_id),
    )
    f.review_decision(
        db, tenant_id, listing_id, version_id, decided_by_user_id=reviewer, batch_id=batch_id
    )
    item_id = f.listing_item(db, tenant_id, listing_id, style, item_role="parent")
    db.execute("UPDATE listing_items SET live_version_id = %s WHERE id = %s", (version_id, item_id))
    run_id = f.validation_run(db, tenant_id, listing_id, listing_version_id=version_id)
    f.agent_run(
        db, tenant_id, product_id=style, listing_id=listing_id, listing_version_id=version_id
    )
    f.performance(db, tenant_id, listing_id)
    document_id = f.brand_document(db, tenant_id, created_by=owner)
    f.brand_chunk(db, tenant_id, source_id=document_id)
    task_id = f.human_task(
        db,
        tenant_id,
        kind="image_replacement",
        product_id=style,
        target_path=None,
        image_id=image_id,
        resume_batch_id=batch_id,
    )
    f.human_task(
        db,
        tenant_id,
        kind="reconnect_account",
        product_id=None,
        target_path="account.token",
        marketplace_account_id=account,
    )
    rule_id = f.rule(db, tenant_id)
    f.listing_issue(
        db,
        tenant_id,
        listing_id,
        listing_item_id=item_id,
        listing_version_id=version_id,
        validation_run_id=run_id,
        rule_id=rule_id,
        human_task_id=task_id,
    )


def _counts(db: Conn, tenant_id: UUID) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in TABLES_0002:
        row = db.execute(
            sql.SQL("SELECT count(*) FROM {} WHERE tenant_id = %s").format(sql.Identifier(table)),
            (tenant_id,),
        ).fetchone()
        assert row is not None
        counts[table] = int(row[0])
    return counts


def test_purge_order_over_full_0002_graph(db: Conn) -> None:
    """DATABASE.md §9: with the pointers deferred, deleting children first and then the tenant
    removes every 0002 row of that tenant and leaves the other tenant's graph whole."""
    tenant_a, tenant_b = f.tenant(db), f.tenant(db)
    _graph(db, tenant_a)
    _graph(db, tenant_b)
    before_b = _counts(db, tenant_b)
    assert all(count > 0 for count in _counts(db, tenant_a).values())
    with db.transaction():
        db.execute("SET CONSTRAINTS ALL DEFERRED")
        for table in PURGE_ORDER:
            db.execute(
                sql.SQL("DELETE FROM {} WHERE tenant_id = %s").format(sql.Identifier(table)),
                (tenant_a,),
            )
        db.execute("DELETE FROM tenants WHERE id = %s", (tenant_a,))
    assert set(_counts(db, tenant_a).values()) == {0}
    assert _counts(db, tenant_b) == before_b


def test_tenant_delete_cascades_whole_0002_graph(db: Conn) -> None:
    """The §1 convention ("tenants(id) cascades, so the final purge is one DELETE") holds for
    every 0002 table: one DELETE (no audit rows, no brand profile) removes the whole graph."""
    tenant_a, tenant_b = f.tenant(db), f.tenant(db)
    _graph(db, tenant_a)
    _graph(db, tenant_b)
    before_b = _counts(db, tenant_b)
    db.execute("DELETE FROM tenants WHERE id = %s", (tenant_a,))
    assert set(_counts(db, tenant_a).values()) == {0}
    assert _counts(db, tenant_b) == before_b


def test_app_rw_writes_every_0002_table(db: Conn) -> None:
    """The app role can write each new table through the 0000 default privileges, including
    `rules.revision` from `rules_revision_seq` (USAGE on the sequence)."""
    tenant_id = f.tenant(db)
    db.execute("SET LOCAL ROLE app_rw")
    _graph(db, tenant_id)
    f.audit(db, tenant_id)
    revisions = db.execute(
        "SELECT revision FROM rules WHERE tenant_id = %s", (tenant_id,)
    ).fetchall()
    assert revisions and all(isinstance(r[0], int) for r in revisions)
    for table in (*TABLES_0002, "audit_log"):
        row = db.execute(
            sql.SQL("SELECT count(*) FROM {} WHERE tenant_id = %s").format(sql.Identifier(table)),
            (tenant_id,),
        ).fetchone()
        assert row is not None and row[0] > 0, table


def test_rules_revision_bumps_on_each_insert(db: Conn) -> None:
    tenant_id = f.tenant(db)
    first = f.rule(db, tenant_id)
    second = f.rule(db, tenant_id, version=2)
    rows: dict[Any, Any] = dict(
        db.execute(
            "SELECT id, revision FROM rules WHERE id IN (%s, %s)", (first, second)
        ).fetchall()
    )
    assert rows[second] > rows[first]


# ---- ON CONFLICT targets the later upserts rely on (PLAN §6 idempotency) ----


def test_li_one_open_is_an_on_conflict_target(db: Conn) -> None:
    tenant_id = f.tenant(db)
    listing_id = f.listing(db, tenant_id)
    first = f.listing_issue(db, tenant_id, listing_id)
    upsert = """
        INSERT INTO listing_issues (tenant_id, listing_id, marketplace, source, field_path,
          error_code, error_class, retry_class, severity, message, dedupe_key)
        SELECT tenant_id, listing_id, marketplace, source, field_path, error_code, error_class,
          retry_class, severity, message, dedupe_key FROM listing_issues WHERE id = %s
        ON CONFLICT (listing_id, dedupe_key) WHERE status = 'open'
        DO UPDATE SET occurrences = listing_issues.occurrences + 1,
          last_seen_at = greatest(listing_issues.last_seen_at, excluded.last_seen_at)
        RETURNING id, occurrences
    """
    for expected in (2, 3):
        row = db.execute(upsert, (first,)).fetchone()
        assert row == (first, expected)
    # a closed issue no longer blocks a new open one on the same key
    db.execute(
        "UPDATE listing_issues SET status = 'resolved', resolution = 'human_edit', "
        "resolved_at = now() WHERE id = %s",
        (first,),
    )
    row = db.execute(upsert, (first,)).fetchone()
    assert row is not None and row[0] != first and row[1] == 1


def test_performance_listing_date_key_is_an_on_conflict_target(db: Conn) -> None:
    tenant_id = f.tenant(db)
    listing_id = f.listing(db, tenant_id)
    upsert = """
        INSERT INTO performance (tenant_id, listing_id, marketplace, date, clicks)
        VALUES (%s, %s, 'amazon', '2026-10-01', %s)
        ON CONFLICT ON CONSTRAINT performance_listing_date_key
        DO UPDATE SET clicks = excluded.clicks RETURNING clicks
    """
    assert db.execute(upsert, (tenant_id, listing_id, 3)).fetchone() == (3,)
    assert db.execute(upsert, (tenant_id, listing_id, 7)).fetchone() == (7,)
    assert db.execute(
        "SELECT count(*) FROM performance WHERE listing_id = %s", (listing_id,)
    ).fetchone() == (1,)


def test_review_approve_replay_hits_the_unique_index(db: Conn) -> None:
    tenant_id = f.tenant(db)
    listing_id = f.listing(db, tenant_id)
    version_id = f.version(db, tenant_id, listing_id)
    reviewer = f.member(db, tenant_id, role="reviewer")
    f.review_decision(db, tenant_id, listing_id, version_id, decided_by_user_id=reviewer)
    row = db.execute(
        "INSERT INTO review_decisions (tenant_id, listing_id, listing_version_id, decision, "
        "decided_by_user_id) VALUES (%s, %s, %s, 'approve', %s) "
        "ON CONFLICT (listing_version_id) WHERE decision = 'approve' DO NOTHING RETURNING id",
        (tenant_id, listing_id, version_id, reviewer),
    ).fetchone()
    assert row is None


# ---- cross-tenant vector search on the year-1 exact path (DATABASE.md §5.5, T66) ----

EXACT = """
    WITH c AS MATERIALIZED (
      SELECT id, tenant_id, embedding FROM brand_chunks
      WHERE tenant_id = %s AND is_active AND embedding_model = %s AND type = %s)
    SELECT id, tenant_id FROM c ORDER BY embedding <=> %s::vector LIMIT 8
"""


def test_exact_search_tiny_tenant_beside_large_one(db: Conn) -> None:
    """The recall rule: a tiny tenant beside a large one gets min(8, its chunk count) hits,
    and only its own, even when the other tenant's chunks sit nearer the query vector."""
    big, tiny = f.tenant(db), f.tenant(db)
    for n in range(300):
        f.brand_chunk(db, big, embedding=f.embedding(0.5 + n / 10000))
    for n in range(3):
        f.brand_chunk(db, tiny, embedding=f.embedding(-0.5 - n / 100))
    f.brand_chunk(db, tiny, embedding=f.embedding(0.5), is_active=False)
    db.execute("ANALYZE brand_chunks")
    for seqscan in ("on", "off"):
        db.execute(f"SET LOCAL enable_seqscan = {seqscan}")
        rows = db.execute(EXACT, (tiny, MODEL, "tone_rule", f.embedding(0.5))).fetchall()
        assert len(rows) == 3
        assert {row[1] for row in rows} == {tiny}
        rows = db.execute(EXACT, (big, MODEL, "tone_rule", f.embedding(-0.5))).fetchall()
        assert len(rows) == 8
        assert {row[1] for row in rows} == {big}


@pytest.mark.parametrize(
    "literal",
    [
        "[" + ",".join(["0.1"] * 3) + "]",
        "[" + ",".join(["0.1"] * 1537) + "]",
        "[NaN" + ",0" * 1535 + "]",
    ],
    ids=["dims_3", "dims_1537", "nan"],
)
def test_brand_chunk_embedding_rejects_bad_vectors(db: Conn, literal: str) -> None:
    tenant_id = f.tenant(db)
    with pytest.raises(psycopg.Error), db.transaction():
        f.brand_chunk(db, tenant_id, embedding=literal)


# ---- numeric edges ----


@pytest.mark.parametrize("value", ["Infinity", "-Infinity", "NaN", "-0.000001"])
def test_agent_runs_cost_usd_rejects_non_finite(db: Conn, value: str) -> None:
    tenant_id = f.tenant(db)
    with pytest.raises(psycopg.Error), db.transaction():
        f.agent_run(db, tenant_id, cost_usd=value)


@pytest.mark.parametrize("value", ["Infinity", "NaN", "-0.01"])
def test_performance_revenue_rejects_non_finite(db: Conn, value: str) -> None:
    tenant_id = f.tenant(db)
    listing_id = f.listing(db, tenant_id)
    with pytest.raises(psycopg.Error), db.transaction():
        f.performance(db, tenant_id, listing_id, revenue=value)


def test_storage_key_rejects_unicode_and_control(db: Conn) -> None:
    tenant_id = f.tenant(db)
    product_id = f.product(db, tenant_id)
    for key in (f"{tenant_id}/images/café.jpg", f"{tenant_id}/images/a\nb.jpg", "x" * 1025):
        expect_sqlstate(
            db,
            "23514",
            "INSERT INTO images (tenant_id, product_id, storage_key, slot) "
            "VALUES (%s, %s, %s, 'secondary')",
            (tenant_id, product_id, key),
            constraint="ck_images_storage_key",
        )


def test_storage_key_same_key_two_tenants(db: Conn) -> None:
    """The key is unique per tenant only: until the tenant prefix lands (T109), two tenants
    can hold rows on one object key. Pinned so a change in either direction is deliberate."""
    a, b = f.tenant(db), f.tenant(db)
    f.image(db, a, f.product(db, a), storage_key="shared/key.jpg")
    f.image(db, b, f.product(db, b), storage_key="shared/key.jpg")
    f.brand_document(db, a, storage_key="shared/doc.pdf")
    f.brand_document(db, b, storage_key="shared/doc.pdf")


# ---- 0002b gate refuses the upgrade end to end ----


def test_0002b_upgrade_refused_on_old_pgvector(
    alembic_env: Any, scratch_db: ScratchDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Done-when 5 through env.py: when the gate fails, `upgrade head` raises ExtensionTooOld,
    builds neither index and leaves the database at 0002."""
    cfg: Config = alembic_env(None)
    command.upgrade(cfg, "0002_listing_pipeline")
    real = migrations.require_extension_version

    def too_old(connection: Any, name: str, minimum: tuple[int, ...], **kw: Any) -> None:
        real(connection, name, (99, 0, 0), **kw)

    monkeypatch.setattr(migrations, "require_extension_version", too_old)
    with pytest.raises(ExtensionTooOld, match=r"^pgvector 0\.8\.5 found; 0002b needs >= 99"):
        command.upgrade(cfg, "head")
    with psycopg.connect(scratch_db.libpq, autocommit=True, connect_timeout=5) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchall() == [
            ("0002_listing_pipeline",)
        ]
        names = conn.execute(
            "SELECT relname FROM pg_class WHERE relname IN "
            "('brand_chunks_embedding_idx', 'brand_chunks_tenant_type_idx')"
        ).fetchall()
        assert names == []
    monkeypatch.setattr(migrations, "require_extension_version", real)
    command.upgrade(cfg, "head")


# ---- guard-must-fail proofs: each catalog test goes red on a broken catalog (rolled back) ----


def test_proof_single_column_tenant_fk_turns_composite_test_red(db: Conn) -> None:
    db.execute("ALTER TABLE performance DROP CONSTRAINT fk_performance_listing_id")
    db.execute(
        "ALTER TABLE performance ADD CONSTRAINT fk_performance_listing_id_plain "
        "FOREIGN KEY (listing_id) REFERENCES listings (id)"
    )
    with pytest.raises(AssertionError, match=r"fk_performance_listing_id_(plain|marketplace)"):
        catalog.test_tenant_fks_composite(db)


def test_proof_nullable_tenant_turns_catalog_test_red(db: Conn) -> None:
    db.execute("ALTER TABLE listing_items ALTER COLUMN tenant_id DROP NOT NULL")
    with pytest.raises(AssertionError, match=r"listing_items\.tenant_id is nullable"):
        catalog.test_tenant_columns_catalog(db)


def test_proof_scope_key_without_type_turns_rules_test_red(db: Conn) -> None:
    db.execute("DROP INDEX rules_scope_key")
    db.execute(
        "CREATE UNIQUE INDEX rules_scope_key ON rules (marketplace, coalesce(category, ''), "
        "field, subject, pack, version, "
        "coalesce(tenant_id, '00000000-0000-0000-0000-000000000000'::uuid))"
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        rules.test_rules_unique_includes_type(db)


# ---- property: the object-key CHECK agrees with its documented rule on arbitrary text ----

_KEY_SHAPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*", re.ASCII)


def _key_ok(key: str) -> bool:
    return (
        _KEY_SHAPE.fullmatch(key) is not None
        and len(key) <= 1024
        and ".." not in key
        and "//" not in key
    )


@settings(
    max_examples=300,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    key=st.one_of(
        st.text(alphabet="aZ09._/-\\:éа \n\t", max_size=12),
        st.text(max_size=8).filter(lambda s: "\x00" not in s),
        st.from_regex(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,20}", fullmatch=True),
    )
)
def test_property_storage_key_check_matches_rule(db: Conn, key: str) -> None:
    row = db.execute(
        "SELECT (%s ~ '^[A-Za-z0-9][A-Za-z0-9._/-]*$' AND char_length(%s) <= 1024 "
        "AND strpos(%s, '..') = 0 AND strpos(%s, '//') = 0)",
        (key, key, key, key),
    ).fetchone()
    assert row is not None
    assert bool(row[0]) is _key_ok(key), repr(key)
    with db.transaction(force_rollback=True):
        tenant_id = f.tenant(db)
        product_id = f.product(db, tenant_id)
        try:
            with db.transaction():
                f.image(db, tenant_id, product_id, storage_key=key)
            accepted = True
        except psycopg.errors.CheckViolation as error:
            assert error.diag.constraint_name == "ck_images_storage_key"
            accepted = False
    assert accepted is _key_ok(key), repr(key)
