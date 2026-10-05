"""`review_decisions` and `listing_items` (DATABASE.md §5.3; KI-A "rejected has no state"; T40,
T45): the decision rules, one approval per version, the version pin, the per-SKU identity and the
item live minimum."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

import psycopg
import pytest

from . import factories as f

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]
DIGEST = b"\x07" * 32
JOB = "00000000-0000-0000-0000-00000000000a"


def _listing_with_version(db: Conn) -> tuple[UUID, UUID, UUID]:
    tenant_id = f.tenant(db)
    listing_id = f.listing(db, tenant_id)
    return tenant_id, listing_id, f.version(db, tenant_id, listing_id)


# ---- review_decisions ----


def test_version_pin_same_listing(db: Conn) -> None:
    """AC 2: a decision that pins a version of another listing fails the `→ lv` FK."""
    tenant_id, listing_a, _ = _listing_with_version(db)
    listing_b = f.listing(db, tenant_id)
    version_b = f.version(db, tenant_id, listing_b)
    f.expect_violation(
        db,
        "23503",
        "fk_review_decisions_listing_version_id",
        lambda: f.review_decision(db, tenant_id, listing_a, version_b),
    )
    f.review_decision(db, tenant_id, listing_b, version_b)


def test_one_approve_per_version(db: Conn) -> None:
    tenant_id, listing_id, version_id = _listing_with_version(db)
    f.review_decision(db, tenant_id, listing_id, version_id, facts_digest=DIGEST)
    f.expect_violation(
        db,
        "23505",
        "uq_review_decisions_listing_version_id",
        lambda: f.review_decision(db, tenant_id, listing_id, version_id),
    )
    rejected = {"decision": "reject", "reason_code": "other", "feedback": "no"}
    f.review_decision(db, tenant_id, listing_id, version_id, **rejected)
    f.review_decision(db, tenant_id, listing_id, version_id, **rejected)


def test_review_decisions_defaults(db: Conn) -> None:
    tenant_id, listing_id, version_id = _listing_with_version(db)
    decision_id = f.review_decision(db, tenant_id, listing_id, version_id)
    row = db.execute(
        "SELECT decided_at = now(), field_paths FROM review_decisions WHERE id = %s",
        (decision_id,),
    ).fetchone()
    assert row == (True, [])


DECISION_REJECTS: dict[str, tuple[dict[str, Any], str]] = {
    "decision": ({"decision": "approved"}, "ck_review_decisions_decision"),
    "reason_code": (
        {"decision": "reject", "reason_code": "ugly", "feedback": "x"},
        "ck_review_decisions_reason_code",
    ),
    "reject without reason": (
        {"decision": "reject", "feedback": "x"},
        "ck_review_decisions_reject_reason",
    ),
    "reject without feedback": (
        {"decision": "reject", "reason_code": "other"},
        "ck_review_decisions_reject_reason",
    ),
    "feedback_2001": (
        {"decision": "reject", "reason_code": "other", "feedback": "x" * 2001},
        "ck_review_decisions_feedback",
    ),
    "instructions on approve": ({"instructions": "shorter"}, "ck_review_decisions_instructions"),
    "instructions_2001": (
        {
            "decision": "regenerate_requested",
            "regeneration_job_id": JOB,
            "instructions": "x" * 2001,
        },
        "ck_review_decisions_instructions",
    ),
    "facts_digest_31": ({"facts_digest": b"\x07" * 31}, "ck_review_decisions_facts_digest"),
    "facts_digest on reject": (
        {"decision": "reject", "reason_code": "other", "feedback": "x", "facts_digest": DIGEST},
        "ck_review_decisions_facts_digest",
    ),
    "regenerate without job": (
        {"decision": "regenerate_requested"},
        "ck_review_decisions_regeneration_job",
    ),
    "job on approve": ({"regeneration_job_id": JOB}, "ck_review_decisions_regeneration_job"),
    "approve without version": (
        {"listing_version_id": None},
        "ck_review_decisions_listing_version",
    ),
}


@pytest.mark.parametrize("case", sorted(DECISION_REJECTS))
def test_checks_reject_review_decisions(db: Conn, case: str) -> None:
    columns, constraint = DECISION_REJECTS[case]
    tenant_id, listing_id, version_id = _listing_with_version(db)
    f.expect_violation(
        db,
        "23514",
        constraint,
        lambda: f.review_decision(db, tenant_id, listing_id, version_id, **columns),
    )


def test_checks_accept_review_decisions(db: Conn) -> None:
    """2000-character feedback and instructions, a 32-byte digest, a reject that regenerates
    (`reject_regenerate`), and a regenerate request on a listing with no version."""
    tenant_id, listing_id, version_id = _listing_with_version(db)
    f.review_decision(db, tenant_id, listing_id, version_id, facts_digest=DIGEST)
    f.review_decision(
        db,
        tenant_id,
        listing_id,
        version_id,
        decision="reject",
        reason_code="policy_violation",
        feedback="x" * 2000,
        field_paths=["content.title"],
        regeneration_job_id=JOB,
        batch_id=f.batch(db, tenant_id, kind="reject"),
    )
    f.review_decision(
        db,
        tenant_id,
        f.listing(db, tenant_id),
        None,
        decision="regenerate_requested",
        regeneration_job_id=JOB,
        instructions="x" * 2000,
    )


# ---- listing_items ----


def _item_args(db: Conn) -> tuple[UUID, UUID, UUID]:
    tenant_id = f.tenant(db)
    product_id = f.product(db, tenant_id)
    return tenant_id, f.listing(db, tenant_id, product_id), product_id


def test_item_seller_sku_unique_per_tenant_marketplace(db: Conn) -> None:
    tenant_a, listing_a, product_a = _item_args(db)
    f.listing_item(db, tenant_a, listing_a, product_a, seller_sku="KURTA-001")
    other_listing = f.listing(db, tenant_a)
    other_product = f.product(db, tenant_a)
    f.expect_violation(
        db,
        "23505",
        "uq_listing_items_tenant_id_marketplace_seller_sku",
        lambda: f.listing_item(db, tenant_a, other_listing, other_product, seller_sku="KURTA-001"),
    )
    tenant_b, listing_b, product_b = _item_args(db)
    f.listing_item(db, tenant_b, listing_b, product_b, seller_sku="KURTA-001")
    flipkart = f.listing(db, tenant_a, product_a, marketplace="flipkart")
    f.listing_item(
        db, tenant_a, flipkart, product_a, marketplace="flipkart", seller_sku="KURTA-001"
    )


def test_item_keys(db: Conn) -> None:
    tenant_id = f.tenant(db)
    style = f.product(db, tenant_id, "style")
    listing_id = f.listing(db, tenant_id, style)
    f.listing_item(db, tenant_id, listing_id, style, item_role="parent")
    child_a = f.product(db, tenant_id, "child", parent=style)
    child_b = f.product(db, tenant_id, "child", parent=style)
    f.listing_item(db, tenant_id, listing_id, child_a, item_role="child")
    f.listing_item(db, tenant_id, listing_id, child_b, item_role="child")
    f.expect_violation(
        db,
        "23505",
        "uq_listing_items_listing_id_product_id",
        lambda: f.listing_item(db, tenant_id, listing_id, child_a, item_role="child"),
    )
    f.expect_violation(
        db,
        "23505",
        "uq_listing_items_one_root",
        lambda: f.listing_item(
            db, tenant_id, listing_id, f.product(db, tenant_id), item_role="standalone"
        ),
    )


def test_item_marketplace_must_match_listing(db: Conn) -> None:
    tenant_id, listing_id, product_id = _item_args(db)
    f.expect_violation(
        db,
        "23503",
        "fk_listing_items_listing_id_marketplace",
        lambda: f.listing_item(db, tenant_id, listing_id, product_id, marketplace="flipkart"),
    )


def test_item_live_version_same_listing(db: Conn) -> None:
    tenant_id, listing_id, product_id = _item_args(db)
    other = f.listing(db, tenant_id)
    other_version = f.version(db, tenant_id, other)
    f.expect_violation(
        db,
        "23503",
        "fk_listing_items_live_version_id",
        lambda: f.listing_item(
            db, tenant_id, listing_id, product_id, live_version_id=other_version
        ),
    )


LIVE_ITEM: dict[str, Any] = {
    "marketplace_state": "live",
    "external_id": "B0TEST",
    "first_submitted_at": "2026-10-05",
    "live_confirmed_at": "2026-10-05",
    "last_synced_at": "2026-10-05",
}
ITEM_REJECTS: dict[str, tuple[dict[str, Any], str]] = {
    "live without external_id": (
        {**LIVE_ITEM, "external_id": None, "with_version": True},
        "ck_item_live_minimum",
    ),
    "live without live_version_id": (LIVE_ITEM, "ck_item_live_minimum"),
    "live without last_synced_at": (
        {**LIVE_ITEM, "last_synced_at": None, "with_version": True},
        "ck_item_live_minimum",
    ),
    "processing never submitted": (
        {"marketplace_state": "processing"},
        "ck_item_live_minimum",
    ),
    "partially_live": (
        {"marketplace_state": "partially_live", "first_submitted_at": "2026-10-05"},
        "ck_listing_items_marketplace_state",
    ),
    "item_role": ({"item_role": "variant"}, "ck_listing_items_item_role"),
    "marketplace": ({"marketplace": "ajio"}, "ck_listing_items_marketplace"),
    "item_live_url_javascript": (
        {"live_url": "javascript:alert(1)"},
        "ck_listing_items_live_url",
    ),
    "item_live_url_http": ({"live_url": "http://www.amazon.in/dp/B0"}, "ck_listing_items_live_url"),
}


@pytest.mark.parametrize("case", sorted(ITEM_REJECTS))
def test_checks_reject_items(db: Conn, case: str) -> None:
    columns, constraint = dict(ITEM_REJECTS[case][0]), ITEM_REJECTS[case][1]
    tenant_id, listing_id, product_id = _item_args(db)
    if columns.pop("with_version", False):
        columns["live_version_id"] = f.version(db, tenant_id, listing_id)
    f.expect_violation(
        db,
        "23514",
        constraint,
        lambda: f.listing_item(db, tenant_id, listing_id, product_id, **columns),
    )


def test_checks_accept_items(db: Conn) -> None:
    tenant_id, listing_id, product_id = _item_args(db)
    version_id = f.version(db, tenant_id, listing_id)
    item = f.listing_item(
        db,
        tenant_id,
        listing_id,
        product_id,
        live_version_id=version_id,
        live_url="https://www.amazon.in/dp/B0TEST",
        **LIVE_ITEM,
    )
    row = db.execute("SELECT marketplace_state FROM listing_items WHERE id = %s", (item,))
    assert row.fetchone() == ("live",)


def test_item_defaults(db: Conn) -> None:
    tenant_id, listing_id, product_id = _item_args(db)
    item = f.listing_item(db, tenant_id, listing_id, product_id)
    row = db.execute("SELECT marketplace_state FROM listing_items WHERE id = %s", (item,))
    assert row.fetchone() == ("not_submitted",)


# ---- cross-tenant FKs ----

Build = Callable[[Conn, UUID, UUID], None]


def _decision_listing(db: Conn, a: UUID, b: UUID) -> None:
    listing_b = f.listing(db, b)
    f.review_decision(db, a, listing_b, f.version(db, b, listing_b))


def _decision_decided_by(db: Conn, a: UUID, b: UUID) -> None:
    listing_a = f.listing(db, a)
    version_a = f.version(db, a, listing_a)
    f.review_decision(db, a, listing_a, version_a, decided_by_user_id=f.member(db, b))


def _decision_batch(db: Conn, a: UUID, b: UUID) -> None:
    listing_a = f.listing(db, a)
    version_a = f.version(db, a, listing_a)
    f.review_decision(db, a, listing_a, version_a, batch_id=f.batch(db, b))


def _item_listing(db: Conn, a: UUID, b: UUID) -> None:
    f.listing_item(db, a, f.listing(db, b), f.product(db, a))


def _item_product(db: Conn, a: UUID, b: UUID) -> None:
    f.listing_item(db, a, f.listing(db, a), f.product(db, b))


CROSS_TENANT: dict[str, tuple[Build, str]] = {
    "review_decisions.listing_id": (_decision_listing, "fk_review_decisions_listing_id"),
    "review_decisions.decided_by_user_id": (
        _decision_decided_by,
        "fk_review_decisions_decided_by_user_id",
    ),
    "review_decisions.batch_id": (_decision_batch, "fk_review_decisions_batch_id"),
    "listing_items.listing_id": (_item_listing, "fk_listing_items_listing_id"),
    "listing_items.product_id": (_item_product, "fk_listing_items_product_id"),
}
CROSS_TENANT_FK_COLUMNS = frozenset(CROSS_TENANT)


@pytest.mark.parametrize("column", sorted(CROSS_TENANT))
def test_0002_cross_tenant_fk_reviews_items(db: Conn, column: str) -> None:
    build, constraint = CROSS_TENANT[column]
    tenant_a, tenant_b = f.tenant(db), f.tenant(db)
    f.expect_violation(db, "23503", constraint, lambda: build(db, tenant_a, tenant_b))
