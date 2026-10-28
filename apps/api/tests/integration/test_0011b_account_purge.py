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

from .conftest import expect_sqlstate
from .integrity_support import as_app_rw, as_migrator
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


def test_purge_waits_25_days_unless_forced(db: Conn) -> None:
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
