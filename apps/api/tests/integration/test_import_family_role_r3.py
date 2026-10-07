"""S13b round-3 tester (AC 17; VAR01/VAR02, DATABASE.md:452): the F4/F5 family-role checks.
A SKU that is both a child and a parent in one file never nests a family; a stored style re-sent
under another theme never loses its matrix; the role checks are tenant-scoped and never refuse a
plain product's update."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from mkos_schemas.errors import ErrorReport

from .conftest import LOCAL_HOSTS
from .imports_support import csv_bytes, import_fixture, imports_db, row, row_results, run_job
from .jobs_support import JobWorld, job_row

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["imports_db"]


def failures(conn: Any, import_id: uuid.UUID) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    for number, status, _sku, _pid, report in row_results(conn, import_id):
        if status == "failed":
            out[number] = [e.error_code.value for e in ErrorReport.model_validate(report).errors]
    return out


async def run(
    jw: JobWorld,
    api: httpx.AsyncClient,
    rows: list[dict[str, str]],
    email: str = "operator@acme.test",
) -> uuid.UUID:
    import_id, job_id = await import_fixture(jw, api, csv_bytes(rows), email)
    await run_job(job_id)
    assert job_row(jw.conn, job_id)["status"] in ("succeeded", "partial")
    return import_id


def nested(conn: Any, tenant: uuid.UUID) -> list[tuple[Any, ...]]:
    """Children whose parent is itself a child, and available cells that disagree with
    their child's parent (VAR01 over the stored rows)."""
    found: list[tuple[Any, ...]] = conn.execute(
        "SELECT 'nested', c.sku, p.sku FROM products c JOIN products p "
        "ON p.id = c.parent_product_id WHERE c.tenant_id = %s AND p.parent_product_id IS NOT NULL "
        "UNION ALL "
        "SELECT 'cell', s.sku, c.sku FROM variant_matrix_cells m "
        "JOIN products s ON s.id = m.style_product_id "
        "JOIN products c ON c.id = m.variant_product_id "
        "WHERE m.tenant_id = %s AND m.availability = 'available' "
        "AND (c.parent_product_id IS DISTINCT FROM m.style_product_id "
        "     OR s.parent_product_id IS NOT NULL) ORDER BY 1, 2, 3",
        [tenant, tenant],
    ).fetchall()
    return found


def member(n: int, sku: str, parent: str, **cells: str) -> dict[str, str]:
    return row(n, seller_sku=sku, product_name="Tee", parent_style_id=parent, **cells)


@pytest.mark.parametrize("child_first", [True, False])
async def test_one_file_child_that_is_also_a_parent_never_nests(
    imports_db: JobWorld, api: httpx.AsyncClient, child_first: bool
) -> None:
    """MID is a child of TOP and the parent of LEAF in the same file: whichever family is
    written first, the file never leaves a child under a child or a cell under a child style."""
    tenant = imports_db.tenant("acme")
    a = member(1, "MID", "TOP", size_name="M")
    b = member(2, "LEAF", "MID", size_name="M")
    import_id = await run(imports_db, api, [a, b] if child_first else [b, a])
    assert nested(imports_db.conn, tenant) == []
    assert failures(imports_db.conn, import_id) != {}


async def test_stored_style_resent_under_another_theme_keeps_its_matrix(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    """A stored size family (TEE: M, L) re-sent with colour-only children: the style keeps its
    theme and size run, and every stored cell still matches its child."""
    tenant = imports_db.tenant("acme")
    style = row(1, seller_sku="TEE", product_name="Tee")
    await run(
        imports_db,
        api,
        [style, member(2, "TEE-M", "TEE", size_name="M"), member(3, "TEE-L", "TEE", size_name="L")],
    )
    query = "SELECT variation_theme, size_run FROM products WHERE tenant_id = %s AND sku = 'TEE'"
    stored = imports_db.conn.execute(query, [tenant]).fetchone()
    assert stored == ("size", ["m", "l"])
    second = await run(
        imports_db,
        api,
        [style, member(4, "TEE-RED", "TEE", color_name="Red")],
    )
    assert imports_db.conn.execute(query, [tenant]).fetchone() == stored
    assert nested(imports_db.conn, tenant) == []
    assert failures(imports_db.conn, second) != {}


async def test_role_checks_read_only_the_callers_tenant(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    """bharat's stored family (TEE with child SKU-00001) never refuses acme's rows that reuse the
    same SKUs as a plain row (F5 check) or as a new style key (F4 check)."""
    bharat, acme = imports_db.tenant("bharat"), imports_db.tenant("acme")
    await run(
        imports_db,
        api,
        [
            row(1, seller_sku="TEE", product_name="Tee"),
            member(2, "SKU-00001", "TEE", size_name="M"),
        ],
        "operator@bharat.test",
    )
    plain = await run(imports_db, api, [row(1, seller_sku="TEE")])
    family = await run(imports_db, api, [member(2, "KID", "SKU-00001", size_name="M")])
    assert failures(imports_db.conn, plain) == {}
    assert failures(imports_db.conn, family) == {}
    got = imports_db.conn.execute(
        "SELECT sku, parent_product_id IS NULL, variation_theme FROM products "
        "WHERE tenant_id = %s ORDER BY sku",
        [acme],
    ).fetchall()
    assert got == [("KID", False, None), ("SKU-00001", True, "size"), ("TEE", True, None)]
    assert nested(imports_db.conn, bharat) == []


async def test_plain_product_update_still_imports(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    """The F5 check refuses a family member only: a stored plain product re-sent with a new title
    imports and is updated."""
    tenant = imports_db.tenant("acme")
    await run(imports_db, api, [row(1)])
    second = await run(imports_db, api, [row(1, product_name="Cotton Kurta v2")])
    assert failures(imports_db.conn, second) == {}
    assert imports_db.conn.execute(
        "SELECT title FROM products WHERE tenant_id = %s AND sku = 'SKU-00001'", [tenant]
    ).fetchone() == ("Cotton Kurta v2",)
