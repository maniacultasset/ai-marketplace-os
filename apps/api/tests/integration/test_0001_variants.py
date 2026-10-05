"""`product_colourways` and `variant_matrix_cells` (DATABASE.md §5.2)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
import pytest

from . import factories as f
from .conftest import expect_sqlstate

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]

# Every `→ t` and same-style column here; test_0001_catalog checks the union of these lists.
CROSS_TENANT_FK_COLUMNS = [
    "product_colourways.style_product_id",
    "variant_matrix_cells.style_product_id",
    "variant_matrix_cells.colourway_id",
    "variant_matrix_cells.variant_product_id",
]


def _colour(name: str) -> dict[str, Any]:
    return {"color": f.fact(value=name)}


def _family(conn: Conn, tenant_id: UUID) -> tuple[UUID, UUID, UUID]:
    """A style, one of its colourways, and a child under it."""
    style = f.product(conn, tenant_id, "style")
    colourway = f.colourway(conn, tenant_id, style)
    child = f.product(conn, tenant_id, "child", parent=style)
    return style, colourway, child


def _available(conn: Conn, tenant_id: UUID, style: UUID, child: UUID, **columns: Any) -> UUID:
    values: dict[str, Any] = {
        "availability": "available",
        "unavailable_reason": None,
        "variant_product_id": child,
    }
    return f.cell(conn, tenant_id, style, **{**values, **columns})


def test_colourways_colour_key_collides(db: Conn) -> None:
    tenant_id = f.tenant(db)
    style = f.product(db, tenant_id, "style")
    first = f.colourway(db, tenant_id, style, attributes=_colour("Jet Black"))
    row = db.execute("SELECT colour_key FROM product_colourways WHERE id = %s", (first,)).fetchone()
    assert row == ("jet black",)
    with pytest.raises(psycopg.errors.UniqueViolation) as info, db.transaction():
        f.colourway(db, tenant_id, style, attributes=_colour("jet black "))
    assert info.value.diag.constraint_name == (
        "uq_product_colourways_tenant_id_style_product_id_colour_key"
    )
    f.colourway(db, tenant_id, f.product(db, tenant_id, "style"), attributes=_colour("Jet Black"))
    db.execute("UPDATE product_colourways SET deleted_at = now() WHERE id = %s", (first,))
    f.colourway(db, tenant_id, style, attributes=_colour("Jet Black"))
    f.colourway(db, tenant_id, style, attributes=_colour("Onyx"))  # distinct even if both black


@pytest.mark.parametrize(
    ("attributes", "constraint"),
    [
        ({"pattern": f.fact()}, "ck_product_colourways_attributes"),
        ({"color": f.fact(value=None, normalized="black")}, "ck_product_colourways_attributes"),
        ({"color": {**f.fact(), "value": None}}, "ck_product_colourways_attributes"),
        ({"color": f.fact(value=5)}, "ck_product_colourways_attributes"),
        ({"color": f.fact(value="   ")}, "ck_product_colourways_colour_key"),
        ({"color": f.fact(value="x" * 65)}, "ck_product_colourways_colour_key"),
        ({"color": f.fact("ai", confidence=None)}, "ck_product_colourways_attributes"),
    ],
    ids=["no color", "no value", "JSON null", "number", "blank", "65 chars", "bad fact"],
)
def test_colourways_attributes_check(db: Conn, attributes: dict[str, Any], constraint: str) -> None:
    """A colour without a string value is refused either way: its generated `colour_key` is NULL
    (NOT NULL, checked first) or the attributes CHECK fails."""
    tenant_id = f.tenant(db)
    style = f.product(db, tenant_id, "style")
    with pytest.raises(psycopg.errors.IntegrityError) as info, db.transaction():
        f.colourway(db, tenant_id, style, attributes=attributes)
    if info.value.sqlstate == "23502":
        assert constraint == "ck_product_colourways_attributes"
        assert info.value.diag.column_name == "colour_key"
    else:
        assert info.value.sqlstate == "23514"
        assert info.value.diag.constraint_name == constraint


def test_colourways_colour_key_not_null(db: Conn) -> None:
    row = db.execute(
        "SELECT attnotnull, attgenerated FROM pg_attribute "
        "WHERE attrelid = 'product_colourways'::regclass AND attname = 'colour_key'"
    ).fetchone()
    assert row == (True, "s")


def test_colourways_colour_sku_unique_per_tenant(db: Conn) -> None:
    tenant_id = f.tenant(db)
    style = f.product(db, tenant_id, "style")
    first = f.colourway(db, tenant_id, style, colour_sku="TSHIRT-001-BLK")
    with pytest.raises(psycopg.errors.UniqueViolation) as info, db.transaction():
        f.colourway(db, tenant_id, f.product(db, tenant_id, "style"), colour_sku="TSHIRT-001-BLK")
    assert info.value.diag.constraint_name == "uq_product_colourways_tenant_id_colour_sku"
    other = f.tenant(db)
    f.colourway(db, other, f.product(db, other, "style"), colour_sku="TSHIRT-001-BLK")
    db.execute("UPDATE product_colourways SET deleted_at = now() WHERE id = %s", (first,))
    f.colourway(db, tenant_id, style, colour_sku="TSHIRT-001-BLK")
    for bad in ("", "a b", "x" * 65):
        with pytest.raises(psycopg.errors.CheckViolation) as check, db.transaction():
            f.colourway(db, tenant_id, style, colour_sku=bad)
        assert check.value.diag.constraint_name == "ck_product_colourways_colour_sku"


def test_colourways_position_required(db: Conn) -> None:
    tenant_id = f.tenant(db)
    style = f.product(db, tenant_id, "style")
    with pytest.raises(psycopg.errors.NotNullViolation), db.transaction():
        f.colourway(db, tenant_id, style, position=None)


def test_matrix_cells_colourway_of_other_style_rejected(db: Conn) -> None:
    tenant_id = f.tenant(db)
    style, _, _ = _family(db, tenant_id)
    other_style, other_colourway, _ = _family(db, tenant_id)
    f.cell(db, tenant_id, other_style, colourway_id=other_colourway)
    with pytest.raises(psycopg.errors.ForeignKeyViolation) as info, db.transaction():
        f.cell(db, tenant_id, style, colourway_id=other_colourway)
    assert info.value.diag.constraint_name == (
        "fk_variant_matrix_cells_colourway_id_style_product_id"
    )


def test_matrix_cells_unique_nulls_not_distinct(db: Conn) -> None:
    tenant_id = f.tenant(db)
    style = f.product(db, tenant_id, "style")
    f.cell(db, tenant_id, style, size_key="m")
    with pytest.raises(psycopg.errors.UniqueViolation) as info, db.transaction():
        f.cell(db, tenant_id, style, size_key="m")
    assert info.value.diag.constraint_name == "vmc_cell_uq"
    colourway = f.colourway(db, tenant_id, style)
    f.cell(db, tenant_id, style, colourway_id=colourway, size_key=None)
    with pytest.raises(psycopg.errors.UniqueViolation), db.transaction():
        f.cell(db, tenant_id, style, colourway_id=colourway, size_key=None)
    f.cell(db, tenant_id, style, colourway_id=colourway, size_key="m")


def test_matrix_cells_one_axis(db: Conn) -> None:
    tenant_id = f.tenant(db)
    style = f.product(db, tenant_id, "style")
    with pytest.raises(psycopg.errors.CheckViolation) as info, db.transaction():
        f.cell(db, tenant_id, style, size_key=None, colourway_id=None)
    assert info.value.diag.constraint_name == "ck_variant_matrix_cells_one_axis"


@pytest.mark.parametrize(
    ("columns", "constraint"),
    [
        (
            {"availability": "available", "unavailable_reason": None},
            "ck_variant_matrix_cells_available_variant",
        ),
        ({"variant": True}, "ck_variant_matrix_cells_available_variant"),
        ({"unavailable_reason": None}, "ck_variant_matrix_cells_unavailable_reason"),
        (
            {"availability": "available", "variant": True, "unavailable_reason": "discontinued"},
            "ck_variant_matrix_cells_unavailable_reason",
        ),
        ({"unavailable_reason": "sold_out"}, "ck_variant_matrix_cells_unavailable_reason"),
        ({"availability": "Available"}, "ck_variant_matrix_cells_availability"),
    ],
    ids=[
        "available without a variant",
        "unavailable with a variant",
        "unavailable without a reason",
        "available with a reason",
        "unknown reason",
        "availability vocab",
    ],
)
def test_matrix_cells_availability_pairs(
    db: Conn, columns: dict[str, Any], constraint: str
) -> None:
    tenant_id = f.tenant(db)
    style, _, child = _family(db, tenant_id)
    if columns.pop("variant", False):
        columns["variant_product_id"] = child
    with pytest.raises(psycopg.errors.CheckViolation) as info, db.transaction():
        f.cell(db, tenant_id, style, **columns)
    assert info.value.diag.constraint_name == constraint


def test_matrix_cells_derived_only_not_provided(db: Conn) -> None:
    tenant_id = f.tenant(db)
    style, _, child = _family(db, tenant_id)
    f.cell(db, tenant_id, style, source="derived", unavailable_reason="not_provided")
    for columns in (
        {"source": "derived"},  # not_produced
        {"source": "derived", "unavailable_reason": "discontinued"},
    ):
        with pytest.raises(psycopg.errors.CheckViolation) as info, db.transaction():
            f.cell(db, tenant_id, style, **columns)
        assert info.value.diag.constraint_name == "ck_variant_matrix_cells_derived_not_provided"
    with pytest.raises(psycopg.errors.CheckViolation) as info, db.transaction():
        _available(db, tenant_id, style, child, source="derived")
    assert info.value.diag.constraint_name == "ck_variant_matrix_cells_derived_not_provided"
    with pytest.raises(psycopg.errors.CheckViolation) as info, db.transaction():
        f.cell(db, tenant_id, style, source="ai")
    assert info.value.diag.constraint_name == "ck_variant_matrix_cells_source"


@pytest.mark.parametrize("size_key", [" m", "M", "x" * 33, "", "m\n"])
def test_matrix_cells_size_key_pattern(db: Conn, size_key: str) -> None:
    tenant_id = f.tenant(db)
    style = f.product(db, tenant_id, "style")
    with pytest.raises(psycopg.errors.CheckViolation) as info, db.transaction():
        f.cell(db, tenant_id, style, size_key=size_key)
    assert info.value.diag.constraint_name == "ck_variant_matrix_cells_size_key"


def test_matrix_cells_size_key_accepted(db: Conn) -> None:
    tenant_id = f.tenant(db)
    style = f.product(db, tenant_id, "style")
    for size_key in ("s", "xl", "32", "free", "x" * 32, "3/4 y", "10.5", "2xl+"):
        f.cell(db, tenant_id, style, size_key=size_key)


def test_matrix_cells_variant_unique(db: Conn) -> None:
    tenant_id = f.tenant(db)
    style, _, child = _family(db, tenant_id)
    _available(db, tenant_id, style, child, size_key="m")
    with pytest.raises(psycopg.errors.UniqueViolation) as info, db.transaction():
        _available(db, tenant_id, style, child, size_key="l")
    assert info.value.diag.constraint_name == (
        "uq_variant_matrix_cells_tenant_id_variant_product_id"
    )


@pytest.mark.parametrize("column", CROSS_TENANT_FK_COLUMNS)
def test_0001_cross_tenant_fk_variants(db: Conn, column: str) -> None:
    tenant_a, tenant_b = f.tenant(db), f.tenant(db)
    style_a, colourway_a, child_a = _family(db, tenant_a)
    style_b, colourway_b, child_b = _family(db, tenant_b)
    if column == "product_colourways.style_product_id":
        with pytest.raises(psycopg.errors.ForeignKeyViolation) as info, db.transaction():
            f.colourway(db, tenant_a, style_b)
        assert info.value.diag.constraint_name == "fk_product_colourways_style_product_id"
        return
    # the same cell inside tenant A is accepted
    _available(db, tenant_a, style_a, child_a, colourway_id=colourway_a, size_key="m")
    if column == "variant_matrix_cells.style_product_id":
        columns: dict[str, Any] = {"style": style_b, "colourway_id": None}
        wanted = "fk_variant_matrix_cells_style_product_id"
    elif column == "variant_matrix_cells.colourway_id":
        # B's colourway with B's style: the same-style key holds, the composite style FK does not
        columns = {"style": style_b, "colourway_id": colourway_b}
        wanted = "fk_variant_matrix_cells_style_product_id"
    else:
        columns = {
            "style": style_a,
            "variant_product_id": child_b,
            "availability": "available",
            "unavailable_reason": None,
            "size_key": "l",
        }
        wanted = "fk_variant_matrix_cells_variant_product_id"
    style = columns.pop("style")
    with pytest.raises(psycopg.errors.ForeignKeyViolation) as info, db.transaction():
        f.cell(db, tenant_a, style, **columns)
    assert info.value.diag.constraint_name == wanted


def test_matrix_cells_colourway_needs_same_tenant_style(db: Conn) -> None:
    """A's style with B's colourway: the same-style key rejects it (B's colourway belongs to
    B's style), so no cell can pair a colourway with a style of another tenant."""
    tenant_a, tenant_b = f.tenant(db), f.tenant(db)
    style_a = f.product(db, tenant_a, "style")
    _, colourway_b, _ = _family(db, tenant_b)
    with pytest.raises(psycopg.errors.ForeignKeyViolation) as info, db.transaction():
        f.cell(db, tenant_a, style_a, colourway_id=colourway_b)
    assert info.value.diag.constraint_name == (
        "fk_variant_matrix_cells_colourway_id_style_product_id"
    )


def test_matrix_cells_cross_tenant_variant_in_use_is_23503(db: Conn) -> None:
    """B already has a cell for its variant V; a cell in A naming V fails at the FK (23503),
    never at the variant key (23505), which would tell A that B's row exists."""
    tenant_a, tenant_b = f.tenant(db), f.tenant(db)
    style_b, _, child_b = _family(db, tenant_b)
    _available(db, tenant_b, style_b, child_b, size_key="m")
    style_a = f.product(db, tenant_a, "style")
    expect_sqlstate(
        db,
        "23503",
        "INSERT INTO variant_matrix_cells (tenant_id, style_product_id, size_key, availability, "
        "variant_product_id, source) VALUES (%s, %s, 'm', 'available', %s, 'seller')",
        (tenant_a, style_a, child_b),
        constraint="fk_variant_matrix_cells_variant_product_id",
    )


def _victim(conn: Conn) -> tuple[UUID, UUID]:
    """Tenant B's style with a Navy colourway, a Navy/m cell and its Navy/m child."""
    tenant_b = f.tenant(conn)
    style = f.product(conn, tenant_b, "style")
    colourway = f.colourway(conn, tenant_b, style, attributes=_colour("Navy"))
    f.cell(conn, tenant_b, style, colourway_id=colourway, size_key="m")
    f.product(conn, tenant_b, "child", parent=style, variant_key="color=navy|size=m")
    return style, colourway


def _first_error(conn: Conn, write: Any) -> psycopg.Error:
    with pytest.raises(psycopg.Error) as info, conn.transaction():
        write()
    return info.value


@pytest.mark.parametrize(
    ("write", "constraint"),
    [
        (
            lambda db, a, style, cw: f.cell(db, a, style, colourway_id=cw, size_key="m"),
            "fk_variant_matrix_cells_style_product_id",
        ),
        (
            lambda db, a, style, cw: f.colourway(db, a, style, attributes=_colour("navy")),
            "fk_product_colourways_style_product_id",
        ),
        (
            lambda db, a, style, cw: f.product(
                db, a, "child", parent=style, variant_key="color=navy|size=m"
            ),
            "fk_products_parent_product_id",
        ),
    ],
    ids=["cell", "colourway", "child"],
)
def test_0001_cross_tenant_natural_key_is_23503(db: Conn, write: Any, constraint: str) -> None:
    """A write in A that names B's style and repeats B's natural key fails at the composite FK
    (23503), never at the unique key (23505): unique checks run at index insertion, before the
    statement-end FK check, and ignore RLS, so a key without `tenant_id` would reveal that B's
    colour, size or variant exists."""
    style, colourway = _victim(db)
    tenant_a = f.tenant(db)
    error = _first_error(db, lambda: write(db, tenant_a, style, colourway))
    assert error.sqlstate == "23503", f"a unique key answered first: {error}"
    assert error.diag.constraint_name == constraint


def test_0001_unique_keys_on_tenant_tables_include_tenant_id(db: Conn) -> None:
    """Catalog form: every non-PK unique index on a table with `tenant_id` includes it, or is
    led by a uuid row id."""
    rows = db.execute("""
        SELECT c.relname, i.relname
        FROM pg_index x
        JOIN pg_class i ON i.oid = x.indexrelid
        JOIN pg_class c ON c.oid = x.indrelid
        WHERE c.relnamespace = 'public'::regnamespace AND x.indisunique AND NOT x.indisprimary
          AND EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid
                      AND a.attname = 'tenant_id' AND NOT a.attisdropped)
          AND NOT EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid
                          AND a.attname = 'tenant_id' AND a.attnum = ANY (x.indkey))
          -- (id, style_product_id) is the PK plus a column: no wider than the PK oracle
          AND i.relname <> 'uq_product_colourways_id_style_product_id'
          -- global by design (pre-login accept lookup); the value is a sha256 of 32 random bytes
          AND i.relname <> 'uq_invitations_token_hash'
          -- the same for 0003's refresh token and OAuth state: found by hash before any
          -- tenant is known (the callback carries no JWT), each a sha256 of a random secret
          AND i.relname <> 'uq_refresh_tokens_token_hash'
          AND i.relname <> 'uq_oauth_states_state_hash'
          -- global by design (T31: one seller account, one tenant); seller_id comes from the
          -- marketplace OAuth callback, not from the brand
          AND i.relname <> 'uq_marketplace_accounts_marketplace_seller_id'
          -- led by a uuid row id (0002: (product_id, marketplace), (listing_id, version), …):
          -- probing it needs another tenant's random id, so it is no wider than the PK oracle
          AND (SELECT a.atttypid FROM pg_attribute a WHERE a.attrelid = c.oid
               AND a.attnum = x.indkey[0]) IS DISTINCT FROM 'uuid'::regtype
          -- the tenant is in the key as an expression (rules_scope_key: NULL = platform rule)
          AND strpos(pg_get_indexdef(x.indexrelid), 'COALESCE(tenant_id,') = 0
        ORDER BY 1, 2
    """).fetchall()
    assert rows == [], f"unique keys without tenant_id: {rows}"


def test_variant_fks_restrict_deletes(db: Conn) -> None:
    tenant_id = f.tenant(db)
    style, colourway, child = _family(db, tenant_id)
    _available(db, tenant_id, style, child, colourway_id=colourway, size_key="m")
    for table, row_id, constraint in (
        ("products", child, "fk_variant_matrix_cells_variant_product_id"),
        ("product_colourways", colourway, "fk_variant_matrix_cells_colourway_id_style_product_id"),
    ):
        expect_sqlstate(
            db,
            "23503",
            f"DELETE FROM {table} WHERE id = %s",  # noqa: S608 - fixed table names
            (row_id,),
            constraint=constraint,
        )
