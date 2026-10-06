"""0004/13b_matrix_guard (DATABASE.md §5.2 variant_matrix_cells :452, images :480): the cell guard
(VAR01), the deferred complete-matrix check (VAR02, at COMMIT) and the size-chart slot rule."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import UUID

import psycopg
import pytest

from . import factories as f
from .conftest import ScratchDb, expect_sqlstate, head_database

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]

INSERT_CELL = (
    "INSERT INTO variant_matrix_cells (tenant_id, style_product_id, colourway_id, size_key, "
    "availability, unavailable_reason, variant_product_id, source) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, 'seller')"
)


def _style(db: Conn, theme: str = "size_color") -> tuple[UUID, UUID, UUID | None]:
    """A tenant, a style with size_run {s, m} (sized themes), and a Navy colourway (coloured)."""
    tenant = f.tenant(db)
    run = ["s", "m"] if theme in ("size", "size_color") else None
    style = f.product(db, tenant, "style", variation_theme=theme, size_run=run)
    colourway = None
    if theme in ("color", "size_color"):
        colourway = f.colourway(db, tenant, style, attributes={"color": f.fact(value="Navy")})
    return tenant, style, colourway


def _child(db: Conn, tenant: UUID, style: UUID, size: str | None, colour: str | None) -> UUID:
    variant = {k: v for k, v in (("size", size), ("color", colour)) if v is not None}
    attributes = {"size": f.fact(value=size.upper(), normalized=size)} if size else {}
    return f.product(
        db, tenant, "child", parent=style, variant_attributes=variant, attributes=attributes
    )


def _available(
    tenant: UUID, style: UUID, cw: UUID | None, size: str | None, child: UUID
) -> tuple[Any, ...]:
    return (tenant, style, cw, size, "available", None, child)


@pytest.mark.parametrize(
    ("theme", "use_colourway", "size", "ok"),
    [
        ("size_color", True, "s", True),
        ("size_color", False, "s", False),
        ("size_color", True, None, False),
        ("size", False, "s", True),
        ("size", True, "s", False),
        ("color", True, None, True),
        ("color", True, "s", False),
    ],
)
def test_matrix_axes_follow_theme(
    db: Conn, theme: str, use_colourway: bool, size: str | None, ok: bool
) -> None:
    tenant, style, colourway = _style(db, theme)
    if use_colourway and colourway is None:
        colourway = f.colourway(db, tenant, style)
    params = (
        tenant,
        style,
        colourway if use_colourway else None,
        size,
        "unavailable",
        "not_produced",
        None,
    )
    if ok:
        db.execute(INSERT_CELL, params)
    else:
        expect_sqlstate(db, "VAR01", INSERT_CELL, params)


def test_matrix_size_not_in_run(db: Conn) -> None:
    tenant, style, colourway = _style(db)
    expect_sqlstate(
        db,
        "VAR01",
        INSERT_CELL,
        (tenant, style, colourway, "xl", "unavailable", "not_produced", None),
    )


def test_matrix_style_must_be_live(db: Conn) -> None:
    tenant, style, colourway = _style(db)
    db.execute("UPDATE products SET deleted_at = now() WHERE id = %s", (style,))
    expect_sqlstate(
        db,
        "VAR01",
        INSERT_CELL,
        (tenant, style, colourway, "s", "unavailable", "not_produced", None),
    )


def test_matrix_child_attrs_mismatch(db: Conn) -> None:
    tenant, style, colourway = _style(db)
    wrong_colour = _child(db, tenant, style, "s", "red")
    expect_sqlstate(
        db, "VAR01", INSERT_CELL, _available(tenant, style, colourway, "s", wrong_colour)
    )
    wrong_size = _child(db, tenant, style, "m", "navy")
    expect_sqlstate(db, "VAR01", INSERT_CELL, _available(tenant, style, colourway, "s", wrong_size))
    # size m, but attributes.size.normalized is missing
    db.execute("UPDATE products SET attributes = '{}' WHERE id = %s", (wrong_size,))
    expect_sqlstate(db, "VAR01", INSERT_CELL, _available(tenant, style, colourway, "m", wrong_size))
    right = _child(db, tenant, style, "s", "navy")
    db.execute(INSERT_CELL, _available(tenant, style, colourway, "s", right))


def test_size_chart_on_child_refused(db: Conn) -> None:
    tenant, style, _ = _style(db, "size")
    child = _child(db, tenant, style, "s", None)
    f.image(db, tenant, style, slot="size_chart")
    f.image(db, tenant, f.product(db, tenant), slot="size_chart")
    expect_sqlstate(
        db,
        "GRD01",
        "INSERT INTO images (tenant_id, product_id, storage_key, slot) "
        "VALUES (%s, %s, %s, 'size_chart')",
        (tenant, child, f"{tenant}/images/chart.jpg"),
    )


# ---- VAR02 fires at COMMIT: committed transactions on a module-scoped scratch database ----


@pytest.fixture(scope="module")
def matrix_db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ScratchDb]:
    with head_database(tmp_path_factory.mktemp("matrix")) as scratch:
        yield scratch


def _commit(db: ScratchDb, build: Any) -> str | None:
    with psycopg.connect(db.libpq) as conn:
        try:
            build(conn)
            conn.commit()
        except psycopg.Error as error:
            conn.rollback()
            return error.sqlstate
    return None


def test_matrix_live_child_without_cell_at_commit(matrix_db: ScratchDb) -> None:
    def build(conn: Conn) -> None:
        tenant, style, _ = _style(conn)
        _child(conn, tenant, style, "s", "navy")

    assert _commit(matrix_db, build) == "VAR02"


def test_matrix_complete_matrix_commits(matrix_db: ScratchDb) -> None:
    def build(conn: Conn) -> None:
        tenant, style, colourway = _style(conn)
        for size in ("s", "m"):
            child = _child(conn, tenant, style, size, "navy")
            conn.execute(INSERT_CELL, _available(tenant, style, colourway, size, child))

    assert _commit(matrix_db, build) is None


def test_matrix_soft_delete_child_flips_cell(matrix_db: ScratchDb) -> None:
    state: dict[str, UUID] = {}

    def build(conn: Conn) -> None:
        tenant, style, colourway = _style(conn)
        child = _child(conn, tenant, style, "s", "navy")
        conn.execute(INSERT_CELL, _available(tenant, style, colourway, "s", child))
        state.update(child=child)

    assert _commit(matrix_db, build) is None

    def delete_only(conn: Conn) -> None:
        conn.execute("UPDATE products SET deleted_at = now() WHERE id = %s", (state["child"],))

    assert _commit(matrix_db, delete_only) == "VAR02"

    def delete_and_flip(conn: Conn) -> None:
        conn.execute("UPDATE products SET deleted_at = now() WHERE id = %s", (state["child"],))
        conn.execute(
            "UPDATE variant_matrix_cells SET availability = 'unavailable', "
            "unavailable_reason = 'discontinued', variant_product_id = NULL "
            "WHERE variant_product_id = %s",
            (state["child"],),
        )

    assert _commit(matrix_db, delete_and_flip) is None


def test_matrix_flipping_a_live_childs_cell_refused(matrix_db: ScratchDb) -> None:
    state: dict[str, UUID] = {}

    def build(conn: Conn) -> None:
        tenant, style, colourway = _style(conn)
        child = _child(conn, tenant, style, "s", "navy")
        conn.execute(INSERT_CELL, _available(tenant, style, colourway, "s", child))
        state.update(child=child)

    assert _commit(matrix_db, build) is None

    def flip(conn: Conn) -> None:
        conn.execute(
            "UPDATE variant_matrix_cells SET availability = 'unavailable', "
            "unavailable_reason = 'discontinued', variant_product_id = NULL "
            "WHERE variant_product_id = %s",
            (state["child"],),
        )

    assert _commit(matrix_db, flip) == "VAR02"
