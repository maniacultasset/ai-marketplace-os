"""0011b/01_account_purge (Amazon DPP 1.7): `purge_marketplace_account(t, a, force)`, migrator only,
over two full tenants (the 0002 graph with its Amazon account, and the 0003 graph on a Flipkart
listing and account). Purging the Flipkart account removes that account, its listings' trees, its
submissions and their per-field results, and nothing else: the tenant's Amazon side, its products
and the other tenant are untouched, and one tombstone is written."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg import sql

from . import factories as f
from .conftest import expect_sqlstate
from .integrity_support import as_app_rw, as_migrator
from .ops_support import job_run
from .test_0003_attacks import ALL_TENANT_TABLES, _counts, _full_tenant

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]
PURGE = "SELECT purge_marketplace_account(%s, %s)"
FORCE = "SELECT purge_marketplace_account(%s, %s, force => true)"


def _two_tenants(db: Conn) -> tuple[Any, Any]:
    """Both graphs, built with the submission and item guards off for that set-up only (as the
    tenant purge suite does)."""
    for table, trigger in (("listing_submissions", "ls_guard"), ("listing_items", "li_guard")):
        db.execute(
            sql.SQL("ALTER TABLE {} DISABLE TRIGGER {}").format(
                sql.Identifier(table), sql.Identifier(trigger)
            )
        )
    a, b = _full_tenant(db), _full_tenant(db)
    for table, trigger in (("listing_submissions", "ls_guard"), ("listing_items", "li_guard")):
        db.execute(
            sql.SQL("ALTER TABLE {} ENABLE TRIGGER {}").format(
                sql.Identifier(table), sql.Identifier(trigger)
            )
        )
    return a, b


def _revoke(db: Conn, account: UUID, days: int) -> None:
    db.execute(
        "UPDATE marketplace_accounts SET status = 'revoked', token_enc = NULL, "
        "kms_key_version = NULL, revoked_at = now() - make_interval(days => %s) WHERE id = %s",
        (days, account),
    )


def _count(db: Conn, query: str, *args: Any) -> int:
    row = db.execute(query, args).fetchone()
    assert row is not None
    return int(row[0])


def test_purge_removes_only_the_account_side(db: Conn) -> None:
    a, b = _two_tenants(db)
    before_b = _counts(db, b.tenant_id, ALL_TENANT_TABLES)
    amazon_listings = _count(
        db,
        "SELECT count(*) FROM listings WHERE tenant_id = %s AND marketplace = 'amazon'",
        a.tenant_id,
    )
    products = _count(db, "SELECT count(*) FROM products WHERE tenant_id = %s", a.tenant_id)
    keys = {
        r[0]
        for r in db.execute(
            "SELECT payload_storage_key FROM listing_submissions WHERE marketplace_account_id = %s",
            (a.account_id,),
        ).fetchall()
    }
    assert keys and amazon_listings > 0
    _revoke(db, a.account_id, 26)
    with as_migrator(db):
        result = db.execute(PURGE, (a.tenant_id, a.account_id)).fetchone()
    assert result is not None
    out = result[0]
    assert set(out["object_keys"]) >= keys
    assert out["row_counts"]["marketplace_accounts"] == 1
    assert out["row_counts"]["listings"] >= 1
    assert _count(db, "SELECT count(*) FROM marketplace_accounts WHERE id = %s", a.account_id) == 0
    for table in ("listings", "listing_submissions", "human_tasks"):
        assert (
            _count(
                db,
                sql.SQL("SELECT count(*) FROM {} WHERE marketplace_account_id = %s")
                .format(sql.Identifier(table))
                .as_string(db),
                a.account_id,
            )
            == 0
        ), table
    for table in ("listing_items", "listing_versions", "publish_field_results"):
        assert (
            _count(
                db,
                sql.SQL("SELECT count(*) FROM {} WHERE listing_id = %s")
                .format(sql.Identifier(table))
                .as_string(db),
                a.listing_id,
            )
            == 0
        ), table
    assert (
        _count(
            db,
            "SELECT count(*) FROM listings WHERE tenant_id = %s AND marketplace = 'amazon'",
            a.tenant_id,
        )
        == amazon_listings
    )
    assert _count(db, "SELECT count(*) FROM products WHERE tenant_id = %s", a.tenant_id) == products
    assert _counts(db, b.tenant_id, ALL_TENANT_TABLES) == before_b
    tomb = db.execute(
        "SELECT tenant_id, actor_type, after->>'marketplace', after->'row_counts', after ? 'objects' "
        "FROM audit_log WHERE action = 'marketplace_account.purged' AND entity_id = %s",
        (a.account_id,),
    ).fetchall()
    assert len(tomb) == 1
    assert tomb[0][:3] == (a.tenant_id, "system", "flipkart") and tomb[0][4]
    assert "seller" not in str(tomb[0][3]).lower()


def test_purge_waits_20_days_unless_forced(db: Conn) -> None:
    a, _ = _two_tenants(db)
    with as_migrator(db):
        expect_sqlstate(db, "GRD01", PURGE, (a.tenant_id, a.account_id))  # still active
    _revoke(db, a.account_id, 10)
    with as_migrator(db):
        expect_sqlstate(db, "GRD01", PURGE, (a.tenant_id, a.account_id))  # revoked too recently
        db.execute(FORCE, (a.tenant_id, a.account_id))
    assert _count(db, "SELECT count(*) FROM marketplace_accounts WHERE id = %s", a.account_id) == 0


def test_purge_takes_long_disconnected_accounts(db: Conn) -> None:
    a, _ = _two_tenants(db)
    db.execute(
        "UPDATE marketplace_accounts SET status = 'needs_reconnect', "
        "needs_reconnect_since = now() - interval '26 days' WHERE id = %s",
        (a.account_id,),
    )
    with as_migrator(db):
        db.execute(PURGE, (a.tenant_id, a.account_id))
    assert _count(db, "SELECT count(*) FROM marketplace_accounts WHERE id = %s", a.account_id) == 0


def test_purge_refuses_a_foreign_tenant(db: Conn) -> None:
    a, b = _two_tenants(db)
    _revoke(db, a.account_id, 26)
    with as_migrator(db):
        expect_sqlstate(db, "GRD01", PURGE, (b.tenant_id, a.account_id))


def test_purge_refused_for_app_rw(db: Conn) -> None:
    a, _ = _two_tenants(db)
    with as_app_rw(db, a.tenant_id):
        expect_sqlstate(db, "42501", PURGE, (a.tenant_id, a.account_id))


def test_reconnect_moves_history_and_the_purge_leaves_it(db: Conn) -> None:
    """Revoke, reconnect the same seller (a new account B), the listing and its submissions move
    to B through the app's own path (app_rw, ls_guard 0011b), a retry on B names the moved attempt
    as its previous one; purging the old account then succeeds, B keeps the full history and
    nothing of A is left."""
    a, _ = _two_tenants(db)
    seller = db.execute(
        "SELECT seller_id FROM marketplace_accounts WHERE id = %s", (a.account_id,)
    ).fetchone()
    assert seller is not None
    _revoke(db, a.account_id, 21)
    new = f.marketplace_account(
        db, a.tenant_id, marketplace="flipkart", seller_id=seller[0], connected_by_user_id=a.user_id
    )
    first = db.execute(
        "SELECT id FROM listing_submissions WHERE listing_id = %s ORDER BY attempt LIMIT 1",
        (a.listing_id,),
    ).fetchone()
    assert first is not None
    with as_app_rw(db, a.tenant_id):
        db.execute(
            "UPDATE listings SET marketplace_account_id = %s "
            "WHERE marketplace_account_id = %s AND marketplace = 'flipkart'",
            (new, a.account_id),
        )
        moved = db.execute(
            "UPDATE listing_submissions SET marketplace_account_id = %s "
            "WHERE listing_id = %s AND marketplace_account_id = %s",
            (new, a.listing_id, a.account_id),
        ).rowcount
    assert moved >= 1
    # the retry on B (placed directly: the publish guards are not under test here)
    db.execute("SET LOCAL session_replication_role = replica")
    # the first attempt ended before the retry (one open submission per listing, ux_ls_open)
    db.execute(
        "UPDATE listing_submissions SET outcome = 'rejected', responded_at = now(), resolved_at = now(), "
        "error_report_json = '{}'::jsonb WHERE id = %s",
        (first[0],),
    )
    db.execute(
        "INSERT INTO listing_submissions (tenant_id, listing_id, listing_version_id, marketplace, "
        "marketplace_account_id, job_id, attempt, operation, payload_sha256, payload_storage_key, "
        "facts_digest, idempotency_key, requested_by_user_id, actor_type, previous_submission_id, "
        "retry_justification) "
        "SELECT tenant_id, listing_id, listing_version_id, marketplace, %s, job_id, attempt + 1, "
        "operation, payload_sha256, payload_storage_key || '.retry', facts_digest, "
        "idempotency_key || '-retry', requested_by_user_id, actor_type, id, 'retryable_condition' "
        "FROM listing_submissions WHERE id = %s",
        (new, first[0]),
    )
    db.execute("SET LOCAL session_replication_role = origin")
    history = _count(
        db, "SELECT count(*) FROM listing_submissions WHERE listing_id = %s", a.listing_id
    )
    with as_migrator(db):
        db.execute(PURGE, (a.tenant_id, a.account_id))
    assert (
        _count(
            db,
            "SELECT count(*) FROM listing_submissions WHERE listing_id = %s "
            "AND marketplace_account_id = %s",
            a.listing_id,
            new,
        )
        == history
    )
    assert _count(db, "SELECT count(*) FROM marketplace_accounts WHERE id = %s", a.account_id) == 0
    for table in ("listings", "listing_submissions", "human_tasks"):
        assert (
            _count(
                db,
                sql.SQL("SELECT count(*) FROM {} WHERE marketplace_account_id = %s")
                .format(sql.Identifier(table))
                .as_string(db),
                a.account_id,
            )
            == 0
        ), table


def test_submission_moves_only_to_the_same_seller(db: Conn) -> None:
    """ls_guard 0011b: an account of another seller is refused (SUB03)."""
    a, _ = _two_tenants(db)
    _revoke(db, a.account_id, 21)
    other = f.marketplace_account(
        db, a.tenant_id, marketplace="flipkart", connected_by_user_id=a.user_id
    )
    with as_app_rw(db, a.tenant_id):
        db.execute(
            "UPDATE listings SET marketplace_account_id = %s WHERE id = %s", (other, a.listing_id)
        )
        expect_sqlstate(
            db,
            "SUB03",
            "UPDATE listing_submissions SET marketplace_account_id = %s WHERE listing_id = %s",
            (other, a.listing_id),
        )


def test_a_stale_running_job_never_blocks_the_purge(db: Conn) -> None:
    a, _ = _two_tenants(db)
    _revoke(db, a.account_id, 21)
    fields = {
        "kind": "listing_status_sync",
        "entity_type": "listing",
        "entity_id": a.listing_id,
        "listing_id": a.listing_id,
        "status": "running",
        "attempt": 1,
        "started_at": "2026-10-01T00:00:00Z",
    }
    live = job_run(db, a.tenant_id, lease_expires_at="2099-01-01T00:00:00Z", **fields)
    with as_migrator(db):
        expect_sqlstate(db, "GRD01", PURGE, (a.tenant_id, a.account_id))  # a live lease blocks
    db.execute("SET LOCAL session_replication_role = replica")  # past the job guard, set-up only
    db.execute(
        "UPDATE job_runs SET lease_expires_at = now() - interval '10 minutes' WHERE id = %s",
        (live,),
    )
    db.execute("SET LOCAL session_replication_role = origin")
    with as_migrator(db):
        db.execute(PURGE, (a.tenant_id, a.account_id))  # the expired lease does not
    assert _count(db, "SELECT count(*) FROM marketplace_accounts WHERE id = %s", a.account_id) == 0
