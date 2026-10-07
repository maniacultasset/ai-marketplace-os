"""S13b round-2 tester (AC 17; VAR01/VAR02, DATABASE.md:452): a later file never re-parents a
stored family member silently. A stored style sent as another family's child, and a stored child
sent under another parent, each fail their row and leave every available cell true."""

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


def kid(n: int, parent: str, size: str, **cells: str) -> dict[str, str]:
    return row(n, product_name="Tee", parent_style_id=parent, size_name=size, **cells)


def failures(conn: Any, import_id: uuid.UUID) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    for number, status, _sku, _pid, report in row_results(conn, import_id):
        if status == "failed":
            out[number] = [e.error_code.value for e in ErrorReport.model_validate(report).errors]
    return out


async def run(jw: JobWorld, api: httpx.AsyncClient, rows: list[dict[str, str]]) -> uuid.UUID:
    import_id, job_id = await import_fixture(jw, api, csv_bytes(rows))
    await run_job(job_id)
    assert job_row(jw.conn, job_id)["status"] in ("succeeded", "partial")
    return import_id


def broken_cells(conn: Any, tenant: uuid.UUID) -> list[tuple[Any, ...]]:
    """Available cells whose child is not a child of the cell's style, or whose style is itself
    a child (the VAR01 rule, re-checked over the stored rows)."""
    found: list[tuple[Any, ...]] = conn.execute(
        "SELECT s.sku, c.sku, c.parent_product_id IS NULL, s.parent_product_id IS NOT NULL "
        "FROM variant_matrix_cells m JOIN products s ON s.id = m.style_product_id "
        "JOIN products c ON c.id = m.variant_product_id "
        "WHERE m.tenant_id = %s AND m.availability = 'available' "
        "AND (c.parent_product_id IS DISTINCT FROM m.style_product_id "
        "     OR s.parent_product_id IS NOT NULL) ORDER BY 1, 2",
        [tenant],
    ).fetchall()
    return found


async def test_stored_style_resent_as_a_child_is_a_row_error(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    """A stored style (TEE, with two children) sent as a child of another family becomes a child
    while it still owns its own children and cells: a nested family."""
    tenant = imports_db.tenant("acme")
    await run(imports_db, api, [kid(1, "TEE", "M"), kid(2, "TEE", "L")])
    second = await run(
        imports_db, api, [row(3, seller_sku="TEE", product_name="Tee", parent_style_id="OTHER",
                              size_name="M")],
    )  # fmt: skip
    assert broken_cells(imports_db.conn, tenant) == []
    assert failures(imports_db.conn, second) != {}


async def test_stored_child_moved_to_another_family_keeps_one_cell(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    """A stored child sent under another parent: either a row error or a clean move, never two
    available cells or a cell pointing at a child of another style."""
    tenant = imports_db.tenant("acme")
    await run(imports_db, api, [kid(1, "TEE", "M"), kid(2, "TEE", "L")])
    second = await run(imports_db, api, [kid(1, "TEE2", "M")])
    assert broken_cells(imports_db.conn, tenant) == []
    assert failures(imports_db.conn, second) != {}
