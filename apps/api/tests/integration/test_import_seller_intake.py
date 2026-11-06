"""SI1 T23 (AC17; D9; decision 14): the CSV intake fixes through the real import job. Run in the
Docker batch (`make api-tests-integration`)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

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
