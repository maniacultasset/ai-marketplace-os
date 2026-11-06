"""SI1 T23 (AC17; D9; decision 14): the CSV intake fixes through the real import job. Run in the
Docker batch (`make api-tests-integration`)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest
from mkos_schemas.errors import ErrorReport

from .conftest import LOCAL_HOSTS
from .imports_support import (
    csv_bytes,
    fixture_bytes,
    import_fixture,
    import_row,
    imports_db,
    one,
    row,
    row_results,
    run_job,
)
from .jobs_support import JobWorld, as_member

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["imports_db"]


async def _import(jw: JobWorld, api: httpx.AsyncClient, rows: list[dict[str, str]]) -> Any:
    import_id, job_id = await import_fixture(jw, api, csv_bytes(rows))
    await run_job(job_id)
    return import_id


def _product(jw: JobWorld, sku: str) -> dict[str, Any]:
    cur = jw.conn.execute("SELECT * FROM products WHERE sku = %s", [sku])
    names = [d.name for d in cur.description or ()]
    found = cur.fetchone()
    assert found is not None, sku
    return dict(zip(names, found, strict=True))


async def test_style_number_composition_and_chart_url(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    await _import(
        imports_db,
        api,
        [
            row(
                1,
                style_number="ST-1",
                fabric_composition="60% Cotton, 40% Polyester",
                size_chart="https://cdn.example.com/chart.png",
            )
        ],
    )
    product = _product(imports_db, "SKU-00001")
    assert product["style_number"] == "ST-1"
    fibres = product["attributes"]["fiber_composition"]
    assert fibres["value"] == "60% Cotton, 40% Polyester"
    assert fibres["normalized"] == [
        {"fibre": "cotton", "percent": 60},
        {"fibre": "polyester", "percent": 40},
    ]
    assert product["attributes"]["size_chart_url"]["value"] == "https://cdn.example.com/chart.png"


async def test_bad_composition_row_fails_others_import(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    import_id = await _import(imports_db, api, [row(2, fiber_composition="90% cotton"), row(3)])
    statuses = {r[2]: r[1] for r in row_results(imports_db.conn, import_id)}
    assert statuses == {"SKU-00002": "failed", "SKU-00003": "imported"}


async def test_row_without_image_imports_with_a_warning(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    import_id = await _import(imports_db, api, [row(4, main_image="")])
    (result,) = row_results(imports_db.conn, import_id)
    assert result[1] == "imported"
    report: Any = result[4]
    assert report is None or "upload images in the product form" in str(report)


async def test_empty_media_cell_keeps_stored_urls(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    await _import(imports_db, api, [row(5)])
    first = _product(imports_db, "SKU-00005")["source_image_urls"]
    assert first
    await _import(imports_db, api, [row(5, main_image="")])
    assert _product(imports_db, "SKU-00005")["source_image_urls"] == first
    await _import(imports_db, api, [row(5, main_image="https://cdn.example.com/new.jpg")])
    assert _product(imports_db, "SKU-00005")["source_image_urls"] == [
        "https://cdn.example.com/new.jpg"
    ]


async def test_price_above_mrp_row_refused(imports_db: JobWorld, api: httpx.AsyncClient) -> None:
    import_id = await _import(imports_db, api, [row(6, mrp="999", selling_price="1000")])
    (result,) = row_results(imports_db.conn, import_id)
    assert result[1] == "failed"


def _member(n: int, sku: str, parent: str, **cells: str) -> dict[str, str]:
    return row(n, seller_sku=sku, product_name="Tee", parent_style_id=parent, **cells)


def _results(jw: JobWorld, import_id: Any) -> dict[str, tuple[str, list[tuple[str, str]]]]:
    out: dict[str, tuple[str, list[tuple[str, str]]]] = {}
    for _number, status, sku, _pid, report in row_results(jw.conn, import_id):
        errors = (
            [(e.error_code.value, e.message) for e in ErrorReport.model_validate(report).errors]
            if status == "failed"
            else []
        )
        out[sku] = (status, errors)
    return out


async def test_child_price_above_style_mrp_refused(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    """Round 1 fix (AC3 CSV leg): a child without its own MRP is judged against the style's."""
    import_id = await _import(
        imports_db,
        api,
        [
            _member(10, "AC3TEE", "", mrp="1000", selling_price="500"),
            _member(11, "AC3TEE-M", "AC3TEE", size_name="M", mrp="", selling_price="1200"),
            _member(12, "AC3TEE-L", "AC3TEE", size_name="L", mrp="", selling_price="900"),
            # own MRP, inherits the style's price 500 > 400
            _member(13, "AC3TEE-S", "AC3TEE", size_name="S", mrp="400", selling_price=""),
        ],
    )
    results = _results(imports_db, import_id)
    assert results["AC3TEE"][0] == "imported"
    assert results["AC3TEE-L"][0] == "imported"
    for sku in ("AC3TEE-M", "AC3TEE-S"):
        status, errors = results[sku]
        assert status == "failed", sku
        assert errors[0][0] == "PRICE_ABOVE_MRP"
        assert "selling_price" in errors[0][1]
    stored = imports_db.conn.execute(
        "SELECT count(*) FROM products WHERE sku IN ('AC3TEE-M', 'AC3TEE-S')"
    ).fetchone()
    assert stored is not None and stored[0] == 0


async def test_style_mrp_lowered_under_stored_child_price_refused(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    """Round 1 fix (AC3 CSV leg, reverse): a re-import that lowers the style's MRP under a
    stored child's price that the child does not override fails the style row at mrp."""
    await _import(
        imports_db,
        api,
        [
            _member(20, "AC3POLO", "", mrp="2000", selling_price="500"),
            _member(21, "AC3POLO-M", "AC3POLO", size_name="M", mrp="", selling_price="1500"),
        ],
    )
    assert _product(imports_db, "AC3POLO-M")["selling_price"] == Decimal("1500")
    import_id = await _import(
        imports_db,
        api,
        [
            _member(20, "AC3POLO", "", mrp="1200", selling_price="500"),
            _member(22, "AC3POLO-L", "AC3POLO", size_name="L", mrp="", selling_price="600"),
        ],
    )
    results = _results(imports_db, import_id)
    status, errors = results["AC3POLO"]
    assert status == "failed"
    assert errors[0][0] == "PRICE_ABOVE_MRP"
    assert "mrp" in errors[0][1]
    assert _product(imports_db, "AC3POLO")["mrp"] == Decimal("2000")


async def test_fe_family_fixture_imports(imports_db: JobWorld, api: httpx.AsyncClient) -> None:
    """The FE `buildFamilyCsv` output (no image column) against a style made by POST /products."""
    created = await api.post(
        "/api/v1/products",
        json={"sku": "FE-FAM-1", "title": "FE Family Kurta", "category": "ethnic_wear_kurta_sets"},
        headers=as_member(imports_db, "operator@acme.test"),
    )
    assert created.status_code == 201, created.text
    import_id, job_id = await import_fixture(imports_db, api, fixture_bytes("fe_family.csv"))
    await run_job(job_id)
    results = row_results(imports_db.conn, import_id)
    assert len(results) == 7
    assert {r[1] for r in results} == {"imported"}, results
    assert import_row(imports_db.conn, import_id)["status"] == "succeeded"
    children = one(
        imports_db.conn,
        "SELECT count(*) FROM products WHERE parent_product_id = %s",
        [created.json()["id"]],
    )
    assert children == 6
