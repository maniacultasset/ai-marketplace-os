"""0004/11_li_guard (DATABASE.md §5.3 listing_items :847-848): item role vs product, the seller SKU,
immutability once submitted, and who may write `live`."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from . import factories as f
from .conftest import expect_sqlstate
from .integrity_support import as_app_rw, flow, queued_job, set_gucs

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]

INSERT_ITEM = (
    "INSERT INTO listing_items (tenant_id, listing_id, marketplace, product_id, item_role, "
    "seller_sku) VALUES (%s, %s, 'amazon', %s, %s, %s)"
)


def _sku(db: Conn, product: UUID) -> str:
    row = db.execute("SELECT sku FROM products WHERE id = %s", (product,)).fetchone()
    assert row is not None
    return str(row[0])


def _family(db: Conn) -> tuple[UUID, UUID, UUID, UUID]:
    """A tenant, a style with one child, and an Amazon listing of the style."""
    tenant = f.tenant(db)
    style = f.product(db, tenant, "style")
    child = f.product(db, tenant, "child", parent=style)
    return tenant, style, child, f.listing(db, tenant, style)


def test_li_guard_standalone_ok(db: Conn) -> None:
    tenant = f.tenant(db)
    product = f.product(db, tenant)
    listing = f.listing(db, tenant, product)
    db.execute(INSERT_ITEM, (tenant, listing, product, "standalone", _sku(db, product)))


def test_li_guard_standalone_not_listing_product(db: Conn) -> None:
    tenant = f.tenant(db)
    listing = f.listing(db, tenant)
    other = f.product(db, tenant)
    expect_sqlstate(
        db, "LIT01", INSERT_ITEM, (tenant, listing, other, "standalone", _sku(db, other))
    )


def test_li_guard_standalone_with_live_children(db: Conn) -> None:
    tenant, style, child, listing = _family(db)
    params = (tenant, listing, style, "standalone", _sku(db, style))
    expect_sqlstate(db, "LIT01", INSERT_ITEM, params)
    db.execute("UPDATE products SET deleted_at = now() WHERE id = %s", (child,))
    db.execute(INSERT_ITEM, params)


def test_li_guard_parent_needs_theme(db: Conn) -> None:
    tenant, style, _, listing = _family(db)
    db.execute(INSERT_ITEM, (tenant, listing, style, "parent", _sku(db, style)))
    plain = f.product(db, tenant)
    plain_listing = f.listing(db, tenant, plain)
    expect_sqlstate(
        db, "LIT01", INSERT_ITEM, (tenant, plain_listing, plain, "parent", _sku(db, plain))
    )


def test_li_guard_child_ok(db: Conn) -> None:
    tenant, _, child, listing = _family(db)
    db.execute(INSERT_ITEM, (tenant, listing, child, "child", _sku(db, child)))


def test_li_guard_child_parent_mismatch(db: Conn) -> None:
    tenant, _, _, listing = _family(db)
    other_style = f.product(db, tenant, "style")
    stranger = f.product(db, tenant, "child", parent=other_style)
    expect_sqlstate(
        db, "LIT01", INSERT_ITEM, (tenant, listing, stranger, "child", _sku(db, stranger))
    )


def test_li_guard_child_soft_deleted_refused(db: Conn) -> None:
    tenant, style, child, listing = _family(db)
    second = f.product(db, tenant, "child", parent=style)
    db.execute("UPDATE products SET deleted_at = now() WHERE id = %s", (child,))
    expect_sqlstate(db, "LIT01", INSERT_ITEM, (tenant, listing, child, "child", _sku(db, child)))
    db.execute(INSERT_ITEM, (tenant, listing, second, "child", _sku(db, second)))


def test_li_guard_seller_sku_equals_sku(db: Conn) -> None:
    tenant = f.tenant(db)
    product = f.product(db, tenant)
    listing = f.listing(db, tenant, product)
    expect_sqlstate(db, "LIT01", INSERT_ITEM, (tenant, listing, product, "standalone", "OTHER-SKU"))


def test_li_guard_immutable_once_submitted(db: Conn) -> None:
    tenant = f.tenant(db)
    product = f.product(db, tenant)
    listing = f.listing(db, tenant, product)
    item = f.listing_item(db, tenant, listing, product)
    db.execute("UPDATE listing_items SET seller_sku = 'RENAMED' WHERE id = %s", (item,))
    db.execute(
        "UPDATE listing_items SET first_submitted_at = now(), marketplace_state = 'processing' "
        "WHERE id = %s",
        (item,),
    )
    for assignment in ("seller_sku = 'AGAIN'", "item_role = 'parent'"):
        expect_sqlstate(
            db,
            "LIT01",
            f"UPDATE listing_items SET {assignment} WHERE id = %s",  # noqa: S608
            (item,),
        )
    db.execute("UPDATE listing_items SET external_id = 'B0ITEM' WHERE id = %s", (item,))


def test_li_guard_live_needs_job_guc(db: Conn) -> None:
    fl = flow(db)
    item = f.listing_item(db, fl.tenant_id, fl.listing_id, fl.product_id)
    live = (
        "UPDATE listing_items SET marketplace_state = 'live', external_id = 'B0LIVE', "
        "first_submitted_at = now(), live_confirmed_at = now(), last_synced_at = now(), "
        "live_version_id = %s WHERE id = %s"
    )
    expect_sqlstate(db, "LIT01", live, (fl.v1, item))
    set_gucs(db, job_id=uuid4())  # not a job
    expect_sqlstate(db, "LIT01", live, (fl.v1, item))
    generate = queued_job(db, fl, "listing_generate")
    set_gucs(db, job_id=generate)  # not a publish or sync job
    expect_sqlstate(db, "LIT01", live, (fl.v1, item))
    set_gucs(db, job_id=queued_job(db, fl, "listing_status_sync"))
    db.execute(live, (fl.v1, item))


def test_li_guard_no_delete(db: Conn) -> None:
    tenant = f.tenant(db)
    product = f.product(db, tenant)
    item = f.listing_item(db, tenant, f.listing(db, tenant, product), product)
    with as_app_rw(db):
        expect_sqlstate(db, "42501", "DELETE FROM listing_items WHERE id = %s", (item,))
