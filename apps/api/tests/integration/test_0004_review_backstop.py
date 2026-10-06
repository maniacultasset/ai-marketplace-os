"""0004/07_review_backstop (DATABASE.md §5.3 review_decisions :804, §7.1 per-item savepoints :1794):
a bulk approval (batch of more than one) needs a `validated` listing."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
import pytest

from . import factories as f
from .conftest import expect_sqlstate
from .integrity_support import (
    approve,
    decision,
    flow,
    force_state,
    next_version,
    set_gucs,
    transition,
)

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]

INSERT_APPROVE = (
    "INSERT INTO review_decisions (tenant_id, listing_id, listing_version_id, decision, "
    "decided_by_user_id, batch_id) VALUES (%s, %s, %s, 'approve', %s, %s)"
)


def _batch(db: Conn, tenant: UUID, by: UUID, items: int) -> UUID:
    return f.batch(db, tenant, item_count=items, created_by_user_id=by)


def test_bulk_backstop_needs_validated(db: Conn) -> None:
    fl = flow(db)
    batch = _batch(db, fl.tenant_id, fl.reviewer, 2)
    params = (fl.tenant_id, fl.listing_id, fl.v1, fl.reviewer, batch)
    force_state(db, fl.listing_id, "needs_review")
    expect_sqlstate(db, "REV01", INSERT_APPROVE, params)
    force_state(db, fl.listing_id, "validated")
    db.execute(INSERT_APPROVE, params)


def test_bulk_backstop_reject_unaffected(db: Conn) -> None:
    fl = flow(db)
    force_state(db, fl.listing_id, "needs_review")
    decision(
        db, fl, "reject", fl.v1, fl.reviewer, batch_id=_batch(db, fl.tenant_id, fl.reviewer, 2)
    )


def test_bulk_backstop_single_item_batch_allows(db: Conn) -> None:
    fl = flow(db)
    force_state(db, fl.listing_id, "needs_review")
    batch = _batch(db, fl.tenant_id, fl.reviewer, 1)
    db.execute(INSERT_APPROVE, (fl.tenant_id, fl.listing_id, fl.v1, fl.reviewer, batch))


def test_bulk_backstop_no_batch_allows(db: Conn) -> None:
    fl = flow(db)
    force_state(db, fl.listing_id, "needs_review")
    db.execute(INSERT_APPROVE, (fl.tenant_id, fl.listing_id, fl.v1, fl.reviewer, None))


def test_bulk_backstop_200_items_one_mkc01_one_needs_review(db: Conn) -> None:
    """A 200-item approve, one SAVEPOINT per item: the item the reviewer generated fails MKC01,
    the needs_review item fails REV01, and the other 198 are approved."""
    tenant = f.tenant(db)
    reviewer = f.member(db, tenant, role="reviewer")
    operator = f.member(db, tenant, role="operator")
    account = f.marketplace_account(db, tenant)
    batch = _batch(db, tenant, reviewer, 200)
    items: list[tuple[UUID, UUID]] = []
    for n in range(200):
        product = f.product(db, tenant)
        listing = f.listing(db, tenant, product, marketplace_account_id=account)
        maker = reviewer if n == 7 else operator
        items.append((listing, next_version(db, tenant, listing, None, "generation", maker)))
        force_state(db, listing, "needs_review" if n == 42 else "validated")
    set_gucs(db, user_id=reviewer)
    outcomes: dict[str, list[int]] = {}
    for n, (listing, version) in enumerate(items):
        try:
            with db.transaction():
                db.execute(INSERT_APPROVE, (tenant, listing, version, reviewer, batch))
                approve(db, version, reviewer)
                db.execute(
                    "UPDATE listings SET approved_version_id = %s WHERE id = %s", (version, listing)
                )
                transition(db, listing, "approved", "approve")
            outcomes.setdefault("approved", []).append(n)
        except psycopg.Error as error:
            outcomes.setdefault(str(error.sqlstate), []).append(n)
    assert outcomes.pop("MKC01") == [7]
    assert outcomes.pop("REV01") == [42]
    assert len(outcomes.pop("approved")) == 198
    assert outcomes == {}
    approved = db.execute(
        "SELECT count(*) FROM listings WHERE tenant_id = %s AND status = 'approved'", (tenant,)
    ).fetchone()
    assert approved == (198,)
