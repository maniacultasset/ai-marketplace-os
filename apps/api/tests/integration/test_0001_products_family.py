"""`products` identity and family columns (DATABASE.md §5.2; ADR-0007)."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from . import factories as f
from .integrity_support import disable_0004_guard

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])


@pytest.fixture(autouse=True)
def _0004_guards_off(request: pytest.FixtureRequest) -> None:
    """These suites test 0001's CHECKs and keys on stored values the 0004 variant guard would
    refuse or rewrite first (test_0004_variant_guard.py proves it); the guard is off in the
    rolled-back `db` transaction."""
    if "db" in request.fixturenames:
        disable_0004_guard(request.getfixturevalue("db"), "products", "variant_guard")


Conn = psycopg.Connection[tuple[Any, ...]]

# Every `→ t`/`→ m` column here; test_0001_catalog checks the union of these lists.
CROSS_TENANT_FK_COLUMNS = ["products.parent_product_id", "products.created_by"]

STYLE_OWNED = ["brand_name", "style_number", "sub_category", "product_type", "gender"]
STYLE_OWNED_VALUES = {
    "brand_name": "Acme",
    "style_number": "ST-1",
    "sub_category": "crew_neck",
    "product_type": "t_shirt",
    "gender": "men",
}


def _fails(conn: Conn, error: type[psycopg.Error], constraint: str, **kwargs: Any) -> None:
    with pytest.raises(error) as info, conn.transaction():
        f.product(conn, **kwargs)
    assert info.value.diag.constraint_name == constraint


def test_products_sku_unique_per_tenant(db: Conn) -> None:
    tenant_a, tenant_b = f.tenant(db), f.tenant(db)
    first = f.product(db, tenant_a, sku="TSHIRT-001")
    _fails(
        db,
        psycopg.errors.UniqueViolation,
        "products_tenant_sku_idx",
        tenant_id=tenant_a,
        sku="TSHIRT-001",
    )
    f.product(db, tenant_b, sku="TSHIRT-001")
    db.execute("UPDATE products SET deleted_at = now() WHERE id = %s", (first,))
    # full, not partial: a re-import revives the soft-deleted row instead
    _fails(
        db,
        psycopg.errors.UniqueViolation,
        "products_tenant_sku_idx",
        tenant_id=tenant_a,
        sku="TSHIRT-001",
    )


@pytest.mark.parametrize("column", CROSS_TENANT_FK_COLUMNS)
def test_0001_cross_tenant_fk_products_family(db: Conn, column: str) -> None:
    name = column.split(".")[1]
    tenant_a, tenant_b = f.tenant(db), f.tenant(db)
    if name == "parent_product_id":
        f.product(db, tenant_a, "child", parent=f.product(db, tenant_a, "style"))
        foreign: Any = f.product(db, tenant_b, "style")
        kwargs: dict[str, Any] = {"kind": "child", "parent": foreign}
    else:
        f.product(db, tenant_a, created_by=f.member(db, tenant_a))
        kwargs = {"created_by": f.member(db, tenant_b)}
    _fails(
        db, psycopg.errors.ForeignKeyViolation, f"fk_products_{name}", tenant_id=tenant_a, **kwargs
    )


def test_product_family_parent_delete_restricted(db: Conn) -> None:
    tenant_id = f.tenant(db)
    style = f.product(db, tenant_id, "style")
    f.product(db, tenant_id, "child", parent=style)
    with pytest.raises(psycopg.errors.ForeignKeyViolation) as info, db.transaction():
        db.execute("DELETE FROM products WHERE id = %s", (style,))
    assert info.value.diag.constraint_name == "fk_products_parent_product_id"


def test_product_family_not_own_parent(db: Conn) -> None:
    tenant_id = f.tenant(db)
    product_id = f.product(db, tenant_id, "style")
    with pytest.raises(psycopg.errors.CheckViolation) as info, db.transaction():
        db.execute(
            "UPDATE products SET parent_product_id = id, variation_theme = NULL, "
            'variant_attributes = \'{"size": "m"}\' WHERE id = %s',
            (product_id,),
        )
    assert info.value.diag.constraint_name == "ck_products_not_own_parent"


def test_product_family_theme_only_on_styles(db: Conn) -> None:
    tenant_id = f.tenant(db)
    style = f.product(db, tenant_id, "style")
    _fails(
        db,
        psycopg.errors.CheckViolation,
        "ck_products_theme_on_style",
        tenant_id=tenant_id,
        kind="child",
        parent=style,
        variation_theme="size",
    )
    _fails(
        db,
        psycopg.errors.CheckViolation,
        "ck_products_variation_theme",
        tenant_id=tenant_id,
        kind="style",
        variation_theme="colour",
    )
    for theme in ("size", "color", "size_color"):
        f.product(db, tenant_id, "style", variation_theme=theme)


def test_product_family_attributes_iff_parent(db: Conn) -> None:
    tenant_id = f.tenant(db)
    style = f.product(db, tenant_id, "style")
    _fails(
        db,
        psycopg.errors.CheckViolation,
        "ck_products_attributes_iff_parent",
        tenant_id=tenant_id,
        kind="child",
        parent=style,
        variant_attributes={},
    )
    _fails(
        db,
        psycopg.errors.CheckViolation,
        "ck_products_attributes_iff_parent",
        tenant_id=tenant_id,
        variant_attributes={"size": "m"},
    )
    _fails(
        db,
        psycopg.errors.CheckViolation,
        "ck_products_variant_attributes",
        tenant_id=tenant_id,
        kind="child",
        parent=style,
        variant_attributes=Jsonb(["m"]),
    )


@pytest.mark.parametrize("column", STYLE_OWNED)
def test_product_family_style_owned(db: Conn, column: str) -> None:
    tenant_id = f.tenant(db)
    owned: dict[str, Any] = {column: STYLE_OWNED_VALUES[column]}
    style = f.product(db, tenant_id, "style", **owned)
    _fails(
        db,
        psycopg.errors.CheckViolation,
        "ck_products_style_owned",
        tenant_id=tenant_id,
        kind="child",
        parent=style,
        **owned,
    )


def test_product_family_variant_key_collides(db: Conn) -> None:
    tenant_id = f.tenant(db)
    style, other = f.product(db, tenant_id, "style"), f.product(db, tenant_id, "style")
    first = f.product(db, tenant_id, "child", parent=style, variant_key="color=navy|size=m")
    _fails(
        db,
        psycopg.errors.UniqueViolation,
        "uq_products_tenant_id_parent_product_id_variant_key",
        tenant_id=tenant_id,
        kind="child",
        parent=style,
        variant_key="color=navy|size=m",
    )
    f.product(db, tenant_id, "child", parent=other, variant_key="color=navy|size=m")
    db.execute("UPDATE products SET deleted_at = now() WHERE id = %s", (first,))
    f.product(db, tenant_id, "child", parent=style, variant_key="color=navy|size=m")


@pytest.mark.parametrize(
    "columns",
    [
        {"kind": "style", "size_run": []},
        {"kind": "style", "size_run": [f"s{i}" for i in range(41)]},
        {"kind": "style", "size_run": ["s", None]},
        {"kind": "style", "variation_theme": "color", "size_run": ["s"]},
        {"kind": "standalone", "size_run": ["s"]},
        {"kind": "child", "size_run": ["s"]},
    ],
    ids=["empty", "41", "NULL element", "theme color", "standalone", "on a child"],
)
def test_product_family_size_run(db: Conn, columns: dict[str, Any]) -> None:
    tenant_id = f.tenant(db)
    if columns["kind"] == "child":
        columns["parent"] = f.product(db, tenant_id, "style")
    with pytest.raises(psycopg.errors.CheckViolation) as info, db.transaction():
        f.product(db, tenant_id, **columns)
    assert info.value.diag.constraint_name in ("ck_products_size_run", "ck_products_theme_on_style")


def test_product_family_size_run_accepted(db: Conn) -> None:
    tenant_id = f.tenant(db)
    f.product(db, tenant_id, "style", size_run=["s", "m", "l", "xl"])
    f.product(db, tenant_id, "style", variation_theme="size", size_run=[f"s{i}" for i in range(40)])


PRODUCT_REJECTS: list[tuple[str, dict[str, Any], str]] = [
    ("sku empty", {"sku": ""}, "ck_products_sku"),
    ("sku 65", {"sku": "x" * 65}, "ck_products_sku"),
    ("sku bracket", {"sku": "a]b"}, "ck_products_sku"),
    ("sku space", {"sku": "a b"}, "ck_products_sku"),
    ("sku tab", {"sku": "a\tb"}, "ck_products_sku"),
    ("title empty", {"title": ""}, "ck_products_title"),
    ("title 501", {"title": "x" * 501}, "ck_products_title"),
    ("status Draft", {"status": "Draft"}, "ck_products_status"),
    ("status created", {"status": "created"}, "ck_products_status"),
    ("category A", {"category": "A"}, "ck_products_category"),
    ("category one char", {"category": "a"}, "ck_products_category"),
    ("brand_name 101", {"brand_name": "x" * 101}, "ck_products_brand_name"),
    ("style_number 65", {"style_number": "x" * 65}, "ck_products_style_number"),
    ("sub_category slug", {"sub_category": "Crew Neck"}, "ck_products_sub_category"),
    ("product_type slug", {"product_type": "T-Shirt"}, "ck_products_product_type"),
    ("gender male", {"gender": "male"}, "ck_products_gender"),
    ("stock_ref 129", {"stock_ref": "x" * 129}, "ck_products_stock_ref"),
    ("image urls object", {"source_image_urls": {}}, "ck_products_source_image_urls"),
]


@pytest.mark.parametrize(
    ("columns", "constraint"), [pytest.param(c, k, id=n) for n, c, k in PRODUCT_REJECTS]
)
def test_0001_checks_products_identity(db: Conn, columns: dict[str, Any], constraint: str) -> None:
    _fails(db, psycopg.errors.CheckViolation, constraint, tenant_id=f.tenant(db), **columns)


def test_0001_products_identity_accepts(db: Conn) -> None:
    tenant_id = f.tenant(db)
    f.product(db, tenant_id, sku="Ü-1")
    f.product(db, tenant_id, sku="x" * 64, title="x" * 500, status="active")
    f.product(db, tenant_id, source_image_urls=Jsonb(["https://seller.test/a.jpg"]))
    row = db.execute(
        "SELECT status, source_image_urls, variant_attributes FROM products WHERE id = %s",
        (f.product(db, tenant_id),),
    ).fetchone()
    assert row == ("draft", [], {})
