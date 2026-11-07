"""SI5 T12 (AC17, PLAN D15): barcode aliases and `identifier_exemption` through the real import
job. A style row's exemption reaches its children that give neither a barcode nor their own."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .imports_support import csv_bytes, import_fixture, imports_db, row, row_results, run_job
from .jobs_support import JobWorld

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["imports_db"]


async def _import(jw: JobWorld, api: httpx.AsyncClient, rows: list[dict[str, str]]) -> Any:
    import_id, job_id = await import_fixture(jw, api, csv_bytes(rows))
    await run_job(job_id)
    return import_id


def _identifier(jw: JobWorld, sku: str) -> tuple[Any, ...]:
    found = jw.conn.execute(
        "SELECT identifier_type, identifier_value, identifier_status FROM products WHERE sku = %s",
        [sku],
    ).fetchone()
    assert found is not None, sku
    return tuple(found)


def _member(n: int, sku: str, parent: str, **cells: str) -> dict[str, str]:
    return row(n, seller_sku=sku, product_name="Tee", parent_style_id=parent, **cells)


async def test_style_exemption_reaches_a_child_without_a_barcode(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    import_id = await _import(
        imports_db,
        api,
        [
            _member(20, "IDTEE", "", identifier_exemption="yes"),
            _member(21, "IDTEE-M", "IDTEE", size_name="M"),
            _member(22, "IDTEE-L", "IDTEE", size_name="L", ean="4006381333931"),
        ],
    )
    statuses = {r[2]: r[1] for r in row_results(imports_db.conn, import_id)}
    assert set(statuses.values()) == {"imported"}, statuses
    assert _identifier(imports_db, "IDTEE") == (None, None, None)
    assert _identifier(imports_db, "IDTEE-M") == (None, None, "exempt")
    assert _identifier(imports_db, "IDTEE-L") == ("ean", "4006381333931", "provided")


async def test_standalone_aliases(imports_db: JobWorld, api: httpx.AsyncClient) -> None:
    await _import(
        imports_db,
        api,
        [
            row(30, gtin="10036000291459"),
            row(31, barcode="036000291452"),
            row(32, identifier_exemption="TRUE"),
        ],
    )
    assert _identifier(imports_db, "SKU-00030") == ("gtin", "10036000291459", "provided")
    assert _identifier(imports_db, "SKU-00031") == ("upc", "036000291452", "provided")
    assert _identifier(imports_db, "SKU-00032") == (None, None, "exempt")


async def test_a_twelve_digit_ean_is_a_row_error(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    import_id = await _import(imports_db, api, [row(40, ean="036000291452"), row(41)])
    statuses = {r[2]: r[1] for r in row_results(imports_db.conn, import_id)}
    assert statuses == {"SKU-00040": "failed", "SKU-00041": "imported"}
