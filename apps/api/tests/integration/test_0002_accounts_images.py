"""`marketplace_accounts`, `images` and `batches` (DATABASE.md §5.5 marketplace_accounts, §5.2
images, §5.3 batches): one live account per brand and marketplace, one tenant per live seller
account (T31), the crypto-shred CHECK, image slots, one main image per colourway, object keys,
batch sizes and the cross-tenant FKs."""

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

SLOTS = ["main", "secondary", "lifestyle", "size_chart", "packaging_label"]


# ---- marketplace_accounts ----


def test_one_live_account_per_marketplace(db: Conn) -> None:
    tenant_id = f.tenant(db)
    first = f.marketplace_account(db, tenant_id)
    f.expect_violation(
        db,
        "23505",
        "uq_marketplace_accounts_tenant_id_marketplace",
        lambda: f.marketplace_account(db, tenant_id),
    )
    f.marketplace_account(db, tenant_id, marketplace="flipkart")
    db.execute(
        "UPDATE marketplace_accounts SET status = 'revoked', token_enc = NULL, "
        "kms_key_version = NULL, revoked_at = now() WHERE id = %s",
        (first,),
    )
    f.marketplace_account(db, tenant_id)  # a reconnect after the revoke
    f.marketplace_account(db, f.tenant(db))  # another brand


def test_one_live_tenant_per_seller_account(db: Conn) -> None:
    """T31 default (DATABASE.md §5.5 Index note): a v1 seller account connects to exactly one
    tenant, so two brands never hold live tokens for one (marketplace, seller_id)."""
    first_tenant, second_tenant = f.tenant(db), f.tenant(db)
    first = f.marketplace_account(db, first_tenant, seller_id="A1SELLER")
    for status in ("active", "needs_reconnect"):
        f.expect_violation(
            db,
            "23505",
            "uq_marketplace_accounts_marketplace_seller_id",
            lambda status=status: f.marketplace_account(
                db,
                second_tenant,
                seller_id="A1SELLER",
                status=status,
                needs_reconnect_since="2026-10-05" if status == "needs_reconnect" else None,
            ),
        )
    # the same id on another marketplace is another seller account
    f.marketplace_account(db, second_tenant, marketplace="flipkart", seller_id="A1SELLER")
    db.execute(
        "UPDATE marketplace_accounts SET status = 'revoked', token_enc = NULL, "
        "kms_key_version = NULL, revoked_at = now() WHERE id = %s",
        (first,),
    )
    f.marketplace_account(db, second_tenant, seller_id="A1SELLER")  # after the first brand revoked


MA_REJECTS: dict[str, tuple[dict[str, Any], str]] = {
    "token_required_unless_revoked": (
        {"token_enc": None, "kms_key_version": None},
        "ck_marketplace_accounts_token_required",
    ),
    "ma_reconnect needs since": ({"status": "needs_reconnect"}, "ma_reconnect"),
    "ma_reconnect since without status": ({"needs_reconnect_since": "2026-10-05"}, "ma_reconnect"),
    "settings_too_large": (
        {"settings": {"seller_display_name": "x" * 32768}},
        "ck_marketplace_accounts_settings",
    ),
    "settings not an object": ({"settings": Jsonb([1])}, "ck_marketplace_accounts_settings"),
    "revoked_not_shredded": ({"status": "revoked"}, "ck_ma_revoked_shredded"),
    "revoked keeps the key version": (
        {"status": "revoked", "token_enc": None},
        "ck_ma_revoked_shredded",
    ),
    "status": ({"status": "expired"}, "ck_marketplace_accounts_status"),
    "marketplace": ({"marketplace": "meesho"}, "ck_marketplace_accounts_marketplace"),
}


@pytest.mark.parametrize("case", sorted(MA_REJECTS))
def test_checks_reject_marketplace_accounts(db: Conn, case: str) -> None:
    columns, constraint = MA_REJECTS[case]
    tenant_id = f.tenant(db)
    f.expect_violation(
        db, "23514", constraint, lambda: f.marketplace_account(db, tenant_id, **columns)
    )


def test_checks_accept_marketplace_accounts(db: Conn) -> None:
    tenant_id = f.tenant(db)
    f.marketplace_account(
        db, tenant_id, status="revoked", token_enc=None, kms_key_version=None, revoked_at="now"
    )
    f.marketplace_account(
        db, tenant_id, status="needs_reconnect", needs_reconnect_since="2026-10-05"
    )
    # just under 32 KiB of settings text
    settings = {"seller_display_name": "x" * (32768 - len('{"seller_display_name": ""}'))}
    f.marketplace_account(db, tenant_id, marketplace="flipkart", settings=settings)
    row = db.execute(
        "SELECT octet_length(settings::text) FROM marketplace_accounts WHERE marketplace = "
        "'flipkart' AND tenant_id = %s",
        (tenant_id,),
    ).fetchone()
    assert row == (32768,)


def test_ma_token_enc_is_bytea(db: Conn) -> None:
    row = db.execute(
        "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
        "WHERE attrelid = 'marketplace_accounts'::regclass AND attname = 'token_enc'"
    ).fetchone()
    assert row == ("bytea",)


def test_ma_anchors(db: Conn) -> None:
    rows = db.execute(
        "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = 'marketplace_accounts'::regclass AND contype = 'u' ORDER BY 1"
    ).fetchall()
    assert rows == [
        ("uq_ma_id_marketplace", "UNIQUE (id, marketplace)"),
        ("uq_marketplace_accounts_tenant_id_id", "UNIQUE (tenant_id, id)"),
    ]


# ---- images ----


@pytest.mark.parametrize("slot", SLOTS)
def test_vocab_checks_images_slot(db: Conn, slot: str) -> None:
    tenant_id = f.tenant(db)
    f.image(db, tenant_id, f.product(db, tenant_id), slot=slot)


def test_vocab_checks_images_slot_rejects_swatch(db: Conn) -> None:
    tenant_id = f.tenant(db)
    product = f.product(db, tenant_id)
    f.expect_violation(
        db, "23514", "ck_images_slot", lambda: f.image(db, tenant_id, product, slot="swatch")
    )
    expect_sqlstate(
        db,
        "23502",
        "INSERT INTO images (tenant_id, product_id, storage_key) VALUES (%s, %s, 'k')",
        (tenant_id, product),
    )  # no default slot


def test_images_one_main(db: Conn) -> None:
    tenant_id = f.tenant(db)
    style = f.product(db, tenant_id, "style")
    red, blue = f.colourway(db, tenant_id, style), f.colourway(db, tenant_id, style, position=2)
    f.image(db, tenant_id, style, slot="main")
    f.expect_violation(
        db, "23505", "images_one_main", lambda: f.image(db, tenant_id, style, slot="main")
    )
    f.image(db, tenant_id, style, slot="main", colourway_id=red)
    f.image(db, tenant_id, style, slot="main", colourway_id=blue)
    f.expect_violation(
        db,
        "23505",
        "images_one_main",
        lambda: f.image(db, tenant_id, style, slot="main", colourway_id=red),
    )
    # a soft-deleted main frees the slot
    db.execute(
        "UPDATE images SET deleted_at = now() WHERE product_id = %s AND colourway_id = %s",
        (style, red),
    )
    f.image(db, tenant_id, style, slot="main", colourway_id=red)


def test_images_colourway_of_another_style_rejected(db: Conn) -> None:
    tenant_id = f.tenant(db)
    style, other = f.product(db, tenant_id, "style"), f.product(db, tenant_id, "style")
    colourway = f.colourway(db, tenant_id, other)
    f.expect_violation(
        db,
        "23503",
        "fk_images_colourway_id_product_id",
        lambda: f.image(db, tenant_id, style, colourway_id=colourway),
    )


IMAGE_REJECTS: dict[str, tuple[dict[str, Any], str]] = {
    "status": ({"status": "stored"}, "ck_images_status"),
    "compliance_status": ({"compliance_status": "ok"}, "ck_images_compliance_status"),
    "metadata array": ({"metadata": Jsonb([])}, "ck_images_metadata"),
    "report not an object": ({"image_report_json": Jsonb([])}, "ck_images_image_report_json"),
    "report other key": ({"image_report_json": {"meesho": {}}}, "ck_images_image_report_json"),
    "storage_key url": ({"storage_key": "https://x.test/a.jpg"}, "ck_images_storage_key"),
    "storage_key traversal": ({"storage_key": "t/../b/a.jpg"}, "ck_images_storage_key"),
    "storage_key absolute": ({"storage_key": "/t/a.jpg"}, "ck_images_storage_key"),
    "storage_key double slash": ({"storage_key": "t//a.jpg"}, "ck_images_storage_key"),
    "storage_key 1025": ({"storage_key": "k" * 1025}, "ck_images_storage_key"),
    "position": ({"position": -1}, "ck_images_position"),
    "width": ({"width_px": 0}, "ck_images_dimensions"),
    "bytes": ({"bytes": 0}, "ck_images_bytes"),
    "source_url": ({"source_url": "ftp://x.test/a.jpg"}, "ck_images_source_url"),
}


@pytest.mark.parametrize("case", sorted(IMAGE_REJECTS))
def test_checks_reject_images(db: Conn, case: str) -> None:
    columns, constraint = IMAGE_REJECTS[case]
    tenant_id = f.tenant(db)
    product = f.product(db, tenant_id)
    f.expect_violation(db, "23514", constraint, lambda: f.image(db, tenant_id, product, **columns))


def test_checks_reject_bounds_images(db: Conn) -> None:
    """One tenant never has two rows on one object key (DATABASE.md §5.2 `UNIQUE (tenant_id,
    storage_key)`); a 1024-character key and a report keyed by both marketplaces are accepted."""
    tenant_a, tenant_b = f.tenant(db), f.tenant(db)
    product_a, product_b = f.product(db, tenant_a), f.product(db, tenant_b)
    f.image(db, tenant_a, product_a, storage_key="shared/key.jpg")
    f.expect_violation(
        db,
        "23505",
        "uq_images_tenant_id_storage_key",
        lambda: f.image(db, tenant_a, product_a, storage_key="shared/key.jpg"),
    )
    f.image(db, tenant_b, product_b, storage_key="shared/key.jpg")
    f.image(
        db,
        tenant_a,
        product_a,
        storage_key="k" * 1024,
        image_report_json={"amazon": {}, "flipkart": {}},
        source_url="https://x.test/a.jpg",
        width_px=1000,
        height_px=1000,
        bytes=1,
    )


def test_images_defaults(db: Conn) -> None:
    tenant_id = f.tenant(db)
    image_id = f.image(db, tenant_id, f.product(db, tenant_id))
    row = db.execute(
        "SELECT status, compliance_status, metadata, position FROM images WHERE id = %s",
        (image_id,),
    ).fetchone()
    assert row == ("uploaded", "pending", {}, 0)


# ---- batches ----


@pytest.mark.parametrize(("count", "ok"), [(0, False), (1, True), (200, True), (201, False)])
def test_batches_item_count_bounds(db: Conn, count: int, ok: bool) -> None:
    tenant_id = f.tenant(db)
    if ok:
        f.batch(db, tenant_id, item_count=count)
    else:
        f.expect_violation(
            db,
            "23514",
            "ck_batches_item_count",
            lambda: f.batch(db, tenant_id, item_count=count),
        )


def test_checks_reject_batches_kind(db: Conn) -> None:
    tenant_id = f.tenant(db)
    for kind in ("generate", "regenerate", "approve", "reject", "publish", "status_refresh"):
        f.batch(db, tenant_id, kind=kind)
    f.expect_violation(db, "23514", "ck_batches_kind", lambda: f.batch(db, tenant_id, kind="sync"))
    import_id = "00000000-0000-0000-0000-000000000009"
    f.batch(db, tenant_id, kind="generate", import_id=import_id)
    f.expect_violation(
        db,
        "23514",
        "ck_batches_import_id",
        lambda: f.batch(db, tenant_id, kind="publish", import_id=import_id),
    )


# ---- cross-tenant FKs: every tenant-composite FK column of these tables ----

Build = Callable[[Conn, UUID, UUID], None]


def _ma_connected_by(db: Conn, a: UUID, b: UUID) -> None:
    f.marketplace_account(db, a, connected_by_user_id=f.member(db, b))


def _ma_settings_by(db: Conn, a: UUID, b: UUID) -> None:
    f.marketplace_account(db, a, settings_updated_by=f.member(db, b))


def _image_product(db: Conn, a: UUID, b: UUID) -> None:
    f.image(db, a, f.product(db, b))


def _image_colourway(db: Conn, a: UUID, b: UUID) -> None:
    style_b = f.product(db, b, "style")
    f.image(db, a, f.product(db, a, "style"), colourway_id=f.colourway(db, b, style_b))


def _batch_created_by(db: Conn, a: UUID, b: UUID) -> None:
    f.batch(db, a, created_by_user_id=f.member(db, b))


CROSS_TENANT: dict[str, tuple[Build, str]] = {
    "marketplace_accounts.connected_by_user_id": (
        _ma_connected_by,
        "fk_marketplace_accounts_connected_by_user_id",
    ),
    "marketplace_accounts.settings_updated_by": (
        _ma_settings_by,
        "fk_marketplace_accounts_settings_updated_by",
    ),
    "images.product_id": (_image_product, "fk_images_product_id"),
    "images.colourway_id": (_image_colourway, "fk_images_colourway_id"),
    "batches.created_by_user_id": (_batch_created_by, "fk_batches_created_by_user_id"),
}
CROSS_TENANT_FK_COLUMNS = frozenset(CROSS_TENANT)


@pytest.mark.parametrize("column", sorted(CROSS_TENANT))
def test_0002_cross_tenant_fk_accounts_images(db: Conn, column: str) -> None:
    build, constraint = CROSS_TENANT[column]
    tenant_a, tenant_b = f.tenant(db), f.tenant(db)
    f.expect_violation(db, "23503", constraint, lambda: build(db, tenant_a, tenant_b))
