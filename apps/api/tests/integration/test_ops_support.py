"""The S8 test support: `expect_sqlstate` (conftest) as the 0003 tests use it, and the two-tenant
seed graph every 0003 table test builds on."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from .conftest import expect_sqlstate
from .ops_support import seed_graph

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]


def test_expect_sqlstate_fails_when_no_error(db: Conn) -> None:
    with pytest.raises(AssertionError, match="succeeded"):
        expect_sqlstate(db, "23514", "SELECT 1")


def test_expect_sqlstate_keeps_tx_usable(db: Conn) -> None:
    db.execute("CREATE TEMP TABLE ops_probe (n int CHECK (n > 0))")
    db.execute("INSERT INTO ops_probe VALUES (1)")
    expect_sqlstate(db, "23514", "INSERT INTO ops_probe VALUES (0)")
    # the failed statement rolled back to its savepoint only: the outer transaction still works
    assert db.execute("SELECT count(*) FROM ops_probe").fetchone() == (1,)


def test_seed_graph_two_tenants(db: Conn) -> None:
    seed = seed_graph(db)
    assert seed.a.tenant_id != seed.b.tenant_id
    for side in (seed.a, seed.b):
        row = db.execute(
            "SELECT l.tenant_id, l.marketplace, l.marketplace_account_id, v.listing_id, "
            "i.listing_id, b.tenant_id, m.role, a.marketplace "
            "FROM listings l JOIN listing_versions v ON v.id = %s "
            "JOIN listing_items i ON i.id = %s JOIN batches b ON b.id = %s "
            "JOIN tenant_memberships m ON m.tenant_id = l.tenant_id AND m.user_id = %s "
            "JOIN marketplace_accounts a ON a.id = %s WHERE l.id = %s",
            (
                side.version_id,
                side.item_id,
                side.batch_id,
                side.user_id,
                side.account_id,
                side.listing_id,
            ),
        ).fetchone()
        assert row == (
            side.tenant_id,
            "amazon",
            side.account_id,
            side.listing_id,
            side.listing_id,
            side.tenant_id,
            "owner",
            "amazon",
        )
