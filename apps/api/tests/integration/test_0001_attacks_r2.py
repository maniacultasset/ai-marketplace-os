"""Round-2 tester attacks on the variant natural keys (fix 66a0cde): the UPDATE path, the app_rw
role, and a proof that the old key shape really is the 23505 oracle the regression test guards."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

import psycopg
import pytest

from . import factories as f
from .conftest import expect_sqlstate

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]


def _colour(name: str) -> dict[str, Any]:
    return {"color": f.fact(value=name)}


def _victim(conn: Conn) -> tuple[UUID, UUID]:
    """Tenant B's style with a Navy colourway, a Navy/m cell and its Navy/m child."""
    tenant_b = f.tenant(conn)
    style = f.product(conn, tenant_b, "style")
    colourway = f.colourway(conn, tenant_b, style, attributes=_colour("Navy"))
    f.cell(conn, tenant_b, style, colourway_id=colourway, size_key="m")
    f.product(conn, tenant_b, "child", parent=style, variant_key="color=navy|size=m")
    return style, colourway


def test_update_onto_other_tenants_colour_key_is_23503(db: Conn) -> None:
    """A's own colourway re-pointed at B's style with B's colour: FK first, never the key."""
    b_style, _ = _victim(db)
    a = f.tenant(db)
    mine = f.colourway(db, a, f.product(db, a, "style"), attributes=_colour("Navy"))
    db.execute("SET LOCAL ROLE app_rw")
    expect_sqlstate(
        db,
        "23503",
        "UPDATE product_colourways SET style_product_id = %s WHERE id = %s",
        (b_style, mine),
        constraint="fk_product_colourways_style_product_id",
    )


def test_update_onto_other_tenants_cell_is_23503(db: Conn) -> None:
    """A's own m cell re-pointed at B's style and B's colourway: FK first, never vmc_cell_uq."""
    b_style, b_colourway = _victim(db)
    a = f.tenant(db)
    mine = f.cell(db, a, f.product(db, a, "style"), size_key="m")
    db.execute("SET LOCAL ROLE app_rw")
    error = expect_sqlstate(
        db,
        "23503",
        "UPDATE variant_matrix_cells SET style_product_id = %s, colourway_id = %s WHERE id = %s",
        (b_style, b_colourway, mine),
    )
    assert (error.diag.constraint_name or "").startswith("fk_variant_matrix_cells_")


def test_update_onto_other_tenants_variant_key_is_23503(db: Conn) -> None:
    """A's own child re-parented under B's style with B's variant key: FK first."""
    b_style, _ = _victim(db)
    a = f.tenant(db)
    a_style = f.product(db, a, "style")
    child = f.product(db, a, "child", parent=a_style, variant_key="color=navy|size=m")
    db.execute("SET LOCAL ROLE app_rw")
    expect_sqlstate(
        db,
        "23503",
        "UPDATE products SET parent_product_id = %s WHERE id = %s",
        (b_style, child),
        constraint="fk_products_parent_product_id",
    )


def test_insert_as_app_rw_is_23503(db: Conn) -> None:
    """The app's own role gets the same FK-first answer on all three inserts."""
    b_style, b_colourway = _victim(db)
    a = f.tenant(db)
    db.execute("SET LOCAL ROLE app_rw")
    writes: list[Callable[[], object]] = [
        lambda: f.cell(db, a, b_style, colourway_id=b_colourway, size_key="m"),
        lambda: f.colourway(db, a, b_style, attributes=_colour("navy")),
        lambda: f.product(db, a, "child", parent=b_style, variant_key="color=navy|size=m"),
    ]
    for write in writes:
        with pytest.raises(psycopg.Error) as info, db.transaction():
            write()
        assert info.value.sqlstate == "23503", info.value


@pytest.mark.parametrize(
    ("drop", "old_shape"),
    [
        (
            "DROP INDEX uq_product_colourways_tenant_id_style_product_id_colour_key",
            "CREATE UNIQUE INDEX old_key ON product_colourways (style_product_id, colour_key)"
            " WHERE deleted_at IS NULL",
        ),
        (
            "DROP INDEX uq_products_tenant_id_parent_product_id_variant_key",
            "CREATE UNIQUE INDEX old_key ON products (parent_product_id, variant_key)"
            " WHERE parent_product_id IS NOT NULL AND deleted_at IS NULL",
        ),
        (
            "ALTER TABLE variant_matrix_cells DROP CONSTRAINT vmc_cell_uq",
            "ALTER TABLE variant_matrix_cells ADD CONSTRAINT old_key"
            " UNIQUE NULLS NOT DISTINCT (style_product_id, colourway_id, size_key)",
        ),
    ],
    ids=["colourway", "child", "cell"],
)
def test_old_key_shape_is_the_oracle(db: Conn, drop: str, old_shape: str) -> None:
    """Guard must fail: with the pre-fix key (no tenant_id) swapped in, inside the rolled-back
    transaction, the cross-tenant insert answers 23505 on the key, so the regression test in
    test_0001_variants.py would go red if the key lost tenant_id again."""
    b_style, b_colourway = _victim(db)
    a = f.tenant(db)
    db.execute(drop)
    db.execute(old_shape)
    writes = {
        "product_colourways": lambda: f.colourway(db, a, b_style, attributes=_colour("navy")),
        "products": lambda: f.product(
            db, a, "child", parent=b_style, variant_key="color=navy|size=m"
        ),
        "variant_matrix_cells": lambda: f.cell(
            db, a, b_style, colourway_id=b_colourway, size_key="m"
        ),
    }
    table = next(name for name in writes if name in old_shape)
    with pytest.raises(psycopg.Error) as info, db.transaction():
        writes[table]()
    assert info.value.sqlstate == "23505", info.value
    assert info.value.diag.constraint_name == "old_key"
