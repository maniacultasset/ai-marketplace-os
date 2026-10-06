"""0004/13_variant_guard restore attacks (DATABASE.md §5.2 products :418): a soft-deleted child
never comes back under a style that is deleted, re-categorised, re-themed or made a child in the
same statement or in a concurrent transaction."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import psycopg
import pytest

from . import factories as f
from .conftest import ScratchDb, expect_sqlstate, head_database

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]

STYLE_SET = {
    "style deleted": "deleted_at = now()",
    "category change": "category = 'jeans'",
    "theme change": "variation_theme = 'size'",
    "theme cleared": "variation_theme = NULL",
}


def _deleted_child(db: Conn) -> tuple[UUID, UUID]:
    tenant = f.tenant(db)
    style = f.product(db, tenant, "style", variation_theme="size_color")
    child = f.product(db, tenant, "child", parent=style)
    db.execute("UPDATE products SET deleted_at = now() WHERE id = %s", (child,))
    return style, child


@pytest.mark.parametrize("change", sorted(STYLE_SET))
def test_one_statement_changes_style_and_restores_child(db: Conn, change: str) -> None:
    """One UPDATE that changes the style and restores its child, in either row order."""
    style, child = _deleted_child(db)
    column, value = (part.strip() for part in STYLE_SET[change].split("=", 1))
    if column == "deleted_at":
        query = (
            "UPDATE products SET deleted_at = CASE WHEN id = %(style)s THEN now() ELSE NULL END "
            "WHERE id IN (%(style)s, %(child)s)"
        )
    else:
        query = (
            f"UPDATE products SET {column} = CASE WHEN id = %(style)s THEN {value} "  # noqa: S608
            f"ELSE {column} END, "
            "deleted_at = CASE WHEN id = %(child)s THEN NULL ELSE deleted_at END "
            "WHERE id IN (%(style)s, %(child)s)"
        )
    expect_sqlstate(db, "GRD01", query, {"style": style, "child": child})


@pytest.mark.parametrize("child_first", [True, False])
@pytest.mark.parametrize("change", sorted(STYLE_SET))
def test_cte_changes_style_and_restores_child(db: Conn, change: str, child_first: bool) -> None:
    """Two data-modifying CTE sub-statements share one snapshot; the guard still refuses."""
    style, child = _deleted_child(db)
    restore = "UPDATE products SET deleted_at = NULL WHERE id = %(child)s RETURNING 1"
    alter = (
        f"UPDATE products SET {STYLE_SET[change]} WHERE id = %(style)s RETURNING 1"  # noqa: S608
    )
    first, second = (restore, alter) if child_first else (alter, restore)
    query = f"WITH a AS ({first}), b AS ({second}) SELECT (SELECT count(*) FROM a), (SELECT count(*) FROM b)"  # noqa: S608
    expect_sqlstate(db, "GRD01", query, {"style": style, "child": child})


# ---- two connections: restore vs a concurrent style change ----


@pytest.fixture(scope="module")
def race_db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ScratchDb]:
    with head_database(tmp_path_factory.mktemp("variant_race")) as scratch:
        yield scratch


def _wait_for_lock(libpq: str) -> None:
    with psycopg.connect(libpq, autocommit=True) as probe:
        for _ in range(100):
            row = probe.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'"
            ).fetchone()
            if row and row[0] >= 1:
                return
            time.sleep(0.05)
    raise AssertionError("the second transaction never blocked")


RACE_SET = {
    **STYLE_SET,
    "theme change": "variation_theme = 'color', size_run = NULL",
    "theme cleared": "variation_theme = NULL, size_run = NULL",
}


def _sized_deleted_child(db: Conn) -> tuple[UUID, UUID, UUID]:
    """A `size` style with size_run {m} and its soft-deleted child `m` (no cells yet)."""
    tenant = f.tenant(db)
    style = f.product(db, tenant, "style", variation_theme="size", size_run=["m"])
    child = f.product(
        db,
        tenant,
        "child",
        parent=style,
        variant_attributes={"size": "m"},
        attributes={"size": f.fact(value="M", normalized="m")},
    )
    db.execute("UPDATE products SET deleted_at = now() WHERE id = %s", (child,))
    return tenant, style, child


@pytest.mark.parametrize("restore_first", [True, False])
@pytest.mark.parametrize("change", sorted(RACE_SET))
def test_restore_races_style_change(race_db: ScratchDb, change: str, restore_first: bool) -> None:
    """Restore (with its available matrix cell) against a concurrent style change, as app_rw on
    two connections: whichever commits first, the other is refused GRD01."""
    with psycopg.connect(race_db.libpq) as setup:
        tenant, style, child = _sized_deleted_child(setup)
        setup.commit()

    def restore(conn: Conn) -> None:
        conn.execute("UPDATE products SET deleted_at = NULL WHERE id = %s", (child,))
        conn.execute(
            "INSERT INTO variant_matrix_cells (tenant_id, style_product_id, size_key, "
            "availability, variant_product_id, source) "
            "VALUES (%s, %s, 'm', 'available', %s, 'seller')",
            (tenant, style, child),
        )

    def alter(conn: Conn) -> None:
        query = f"UPDATE products SET {RACE_SET[change]} WHERE id = %s"  # noqa: S608
        conn.execute(query, (style,))

    first, second = (restore, alter) if restore_first else (alter, restore)
    outcome: list[str | None] = []

    def run_second() -> None:
        with psycopg.connect(race_db.libpq) as conn:
            conn.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant)])
            conn.execute("SET ROLE app_rw")
            try:
                second(conn)
                conn.commit()
                outcome.append(None)
            except psycopg.Error as error:
                conn.rollback()
                outcome.append(error.sqlstate)

    with psycopg.connect(race_db.libpq) as one:
        one.execute("SELECT set_config('app.tenant_id', %s, true)", [str(tenant)])
        one.execute("SET ROLE app_rw")
        first(one)
        thread = threading.Thread(target=run_second)
        thread.start()
        _wait_for_lock(race_db.libpq)
        one.commit()
        thread.join(timeout=15)
    assert outcome == ["GRD01"]
    with psycopg.connect(race_db.libpq) as check:
        row = check.execute(
            "SELECT c.deleted_at IS NULL, s.deleted_at IS NULL, s.category, s.variation_theme "
            "FROM products c JOIN products s ON s.id = c.parent_product_id WHERE c.id = %s",
            (child,),
        ).fetchone()
    assert row is not None
    child_live, style_live, category, theme = row
    assert child_live is restore_first
    if child_live:
        assert (style_live, category, theme) == (True, "t_shirts", "size")
