"""0004/13_variant_guard (DATABASE.md §5.2 products :418 variant guard, :423 the variant_key index):
a child sits one level under a live style of its category, carries exactly the theme's axes, and
gets its case-folded `variant_key` from the guard."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg.types.json import Jsonb

from . import factories as f
from .conftest import expect_sqlstate

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]

INSERT_CHILD = (
    "INSERT INTO products (tenant_id, sku, title, category, parent_product_id, "
    "variant_attributes, provenance) VALUES (%s, %s, 'Child', %s, %s, %s, %s)"
)


def _child_params(
    tenant: UUID, style: UUID, sku: str, attributes: dict[str, str], category: str = "t_shirts"
) -> tuple[Any, ...]:
    provenance = {"title": f.fact(), "category": f.fact()}
    return (tenant, sku, category, style, Jsonb(attributes), Jsonb(provenance))


def _style(db: Conn, theme: str = "size_color", **columns: Any) -> tuple[UUID, UUID]:
    tenant = f.tenant(db)
    return tenant, f.product(db, tenant, "style", variation_theme=theme, **columns)


def test_variant_key_casefold_collides(db: Conn) -> None:
    """Moved from S6: `M/Navy` and `m/navy` are one variant."""
    tenant, style = _style(db)
    db.execute(INSERT_CHILD, _child_params(tenant, style, "A-1", {"size": "M", "color": "Navy"}))
    expect_sqlstate(
        db,
        "23505",
        INSERT_CHILD,
        _child_params(tenant, style, "A-2", {"size": " m ", "color": "navy"}),
        constraint="uq_products_tenant_id_parent_product_id_variant_key",
    )


def test_variant_key_written(db: Conn) -> None:
    tenant, style = _style(db)
    db.execute(
        INSERT_CHILD, _child_params(tenant, style, "A-1", {"size": " M ", "color": "Jet Black"})
    )
    row = db.execute(
        "SELECT variant_key FROM products WHERE sku = 'A-1' AND tenant_id = %s", (tenant,)
    ).fetchone()
    assert row == ("color=jet black|size=m",)
    db.execute(
        "UPDATE products SET variant_attributes = %s WHERE sku = 'A-1' AND tenant_id = %s",
        (Jsonb({"size": "L", "color": "Jet Black"}), tenant),
    )
    row = db.execute(
        "SELECT variant_key FROM products WHERE sku = 'A-1' AND tenant_id = %s", (tenant,)
    ).fetchone()
    assert row == ("color=jet black|size=l",)


def test_variant_key_cannot_be_forged(db: Conn) -> None:
    tenant, style = _style(db, theme="size")
    child = f.product(db, tenant, "child", parent=style, variant_attributes={"size": "S"})
    db.execute("UPDATE products SET variant_key = 'size=xl' WHERE id = %s", (child,))
    row = db.execute("SELECT variant_key FROM products WHERE id = %s", (child,)).fetchone()
    assert row == ("size=s",)
    standalone = f.product(db, tenant)
    db.execute("UPDATE products SET variant_key = 'x' WHERE id = %s", (standalone,))
    assert db.execute(
        "SELECT variant_key FROM products WHERE id = %s", (standalone,)
    ).fetchone() == (None,)


def test_variant_parent_other_category(db: Conn) -> None:
    tenant, style = _style(db)
    expect_sqlstate(
        db,
        "GRD01",
        INSERT_CHILD,
        _child_params(tenant, style, "A-1", {"size": "m", "color": "red"}, category="kurtas"),
    )


def test_variant_two_levels_refused(db: Conn) -> None:
    tenant, style = _style(db)
    child = f.product(db, tenant, "child", parent=style)
    expect_sqlstate(
        db,
        "GRD01",
        INSERT_CHILD,
        _child_params(tenant, child, "A-1", {"size": "m", "color": "red"}),
    )


def test_variant_parent_needs_theme_and_live(db: Conn) -> None:
    tenant = f.tenant(db)
    plain = f.product(db, tenant)
    expect_sqlstate(
        db, "GRD01", INSERT_CHILD, _child_params(tenant, plain, "A-1", {"size": "m", "color": "r"})
    )
    _, style = tenant, f.product(db, tenant, "style")
    db.execute("UPDATE products SET deleted_at = now() WHERE id = %s", (style,))
    expect_sqlstate(
        db, "GRD01", INSERT_CHILD, _child_params(tenant, style, "A-2", {"size": "m", "color": "r"})
    )


@pytest.mark.parametrize(
    ("theme", "attributes", "ok"),
    [
        ("size", {"size": "m"}, True),
        ("size", {"size": "m", "color": "red"}, False),
        ("color", {"color": "red"}, True),
        ("color", {"size": "m"}, False),
        ("size_color", {"size": "m", "color": "red"}, True),
        ("size_color", {"size": "m"}, False),
        ("size_color", {"size": "m", "color": "red", "fit": "slim"}, False),
        ("size", {"size": "  "}, False),
    ],
)
def test_variant_keys_match_theme(
    db: Conn, theme: str, attributes: dict[str, str], ok: bool
) -> None:
    tenant, style = _style(db, theme=theme)
    params = _child_params(tenant, style, "A-1", attributes)
    if ok:
        db.execute(INSERT_CHILD, params)
    else:
        expect_sqlstate(db, "GRD01", INSERT_CHILD, params)


def test_variant_style_delete_with_children_refused(db: Conn) -> None:
    tenant, style = _style(db)
    child = f.product(db, tenant, "child", parent=style)
    expect_sqlstate(db, "GRD01", "UPDATE products SET deleted_at = now() WHERE id = %s", (style,))
    db.execute("UPDATE products SET deleted_at = now() WHERE id = %s", (child,))
    db.execute("UPDATE products SET deleted_at = now() WHERE id = %s", (style,))
