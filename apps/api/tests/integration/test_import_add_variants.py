"""Client test B1 (12 Nov): the variants screen's "Save variants" on a product saved with the
guided form. The web builds the family CSV (`buildFamilyCsv`, variants-model.ts) and uploads it
to POST /products/bulk; the import turns the saved standalone into the family's style. A
standalone saved with "No barcode" holds a GTIN exemption, a fact no style may own: before the
fix every row failed with PRV03 `FACT_LEVEL_INVALID`. Run in the Docker batch."""

from __future__ import annotations

import csv
import io
from typing import Any

import httpx
import pytest
from mkos_schemas.enums import Severity
from mkos_schemas.errors import ErrorReport

from .conftest import LOCAL_HOSTS
from .imports_support import MAPPED, import_fixture, import_row, imports_db, row_results, run_job
from .jobs_support import JobWorld, as_member

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["imports_db"]

PRODUCTS = "/api/v1/products"
SKU = "KTEST-QA-001"
TITLE = "KTEST QA Cotton Kurta Set"
# variants-model.ts FAMILY_HEADER, in its order
HEADER = [
    "seller_sku",
    "parent_style_id",
    "product_name",
    "category",
    "color_name",
    "size_name",
    "size_system",
    "mrp",
    "selling_price",
    "product_identifier_type",
    "product_identifier",
    "identifier_exemption",
]
COLOURS = {"Blue": "BLU", "Red": "RED"}
SIZES = ("M", "L", "XL")
CHART = {
    "unit": "cm",
    "basis": "garment",
    "rows": [
        {"size": "M", "chest": 100},
        {"size": "L", "chest": 104},
        {"size": "XL", "chest": 108},
    ],
}


async def _saved(api: httpx.AsyncClient, jw: JobWorld, **body: Any) -> str:
    """The guided form's save: every field filled, "No barcode" unless a barcode is given."""
    payload = {
        "sku": SKU,
        "title": TITLE,
        "category": MAPPED[0],
        "mrp": "1999",
        "selling_price": "1499",
        "size_chart": CHART,
        "identifier_exemption": True,
        **body,
    }
    payload = {k: v for k, v in payload.items() if v is not None}
    created = await api.post(PRODUCTS, json=payload, headers=as_member(jw, "operator@acme.test"))
    assert created.status_code == 201, created.text
    product_id: str = created.json()["id"]
    return product_id


def _family_csv(*, exempt: bool, barcodes: dict[str, str] | None = None) -> bytes:
    """`buildFamilyCsv` for Blue/Red x M/L/XL, each variant with its own price."""
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\r\n")
    w.writerow(HEADER)
    w.writerow([SKU, "", TITLE, MAPPED[0], "", "", "IN", "", "", "", "", "yes" if exempt else ""])
    for colour, code in COLOURS.items():
        for size in SIZES:
            sku = f"{SKU}-{code}-{size}"
            barcode = (barcodes or {}).get(sku, "")
            w.writerow(
                [
                    sku,
                    SKU,
                    TITLE,
                    MAPPED[0],
                    colour,
                    size,
                    "",
                    "1999",
                    "1499",
                    "gtin" if barcode else "",
                    barcode,
                    "",
                ]
            )
    return out.getvalue().encode("utf-8")


async def _import(jw: JobWorld, api: httpx.AsyncClient, data: bytes) -> Any:
    import_id, job_id = await import_fixture(jw, api, data)
    await run_job(job_id)
    return import_id


def _errors(results: list[tuple[Any, ...]]) -> dict[str, list[tuple[str, str]]]:
    """Each failed row's blocking errors as (code, message)."""
    return {
        r[2]: [
            (e.error_code.value, e.message)
            for e in ErrorReport.model_validate(r[4]).errors
            if e.severity == Severity.ERROR
        ]
        for r in results
        if r[1] == "failed"
    }


def _identity(jw: JobWorld, product_id: str) -> list[tuple[Any, ...]]:
    return [
        tuple(r)
        for r in jw.conn.execute(
            "SELECT sku, parent_product_id IS NULL, identifier_status, identifier_value, "
            "provenance ? 'identifier' FROM products WHERE id = %s OR parent_product_id = %s "
            "ORDER BY sku",
            [product_id, product_id],
        ).fetchall()
    ]


@pytest.mark.parametrize("ticked", [False, True], ids=["exemption-from-product", "no-barcodes"])
async def test_add_variants_to_a_product_saved_without_a_barcode(
    imports_db: JobWorld, api: httpx.AsyncClient, ticked: bool
) -> None:
    pid = await _saved(api, imports_db)
    import_id = await _import(imports_db, api, _family_csv(exempt=ticked))
    results = row_results(imports_db.conn, import_id)
    assert len(results) == 7
    assert {r[1] for r in results} == {"imported"}, _errors(results)
    assert import_row(imports_db.conn, import_id)["status"] == "succeeded"
    theme = imports_db.conn.execute(
        "SELECT variation_theme, size_run FROM products WHERE id = %s", [pid]
    ).fetchone()
    assert theme is not None and tuple(theme) == ("size_color", ["m", "l", "xl"])
    # the exemption left the style and reached every new variant
    rows = _identity(imports_db, pid)
    assert rows[0] == (SKU, True, None, None, False)
    assert [r[2:] for r in rows[1:]] == [("exempt", None, True)] * 6
    read = await api.get(f"{PRODUCTS}/{pid}", headers=as_member(imports_db, "operator@acme.test"))
    assert read.status_code == 200, read.text
    assert read.json()["identifier_exemption"] is True


async def test_add_variants_with_their_own_barcodes(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    pid = await _saved(api, imports_db)
    barcodes = {f"{SKU}-BLU-M": "4006381333931", f"{SKU}-RED-XL": "9780306406157"}
    import_id = await _import(imports_db, api, _family_csv(exempt=False, barcodes=barcodes))
    results = row_results(imports_db.conn, import_id)
    assert {r[1] for r in results} == {"imported"}, _errors(results)
    by_sku = {r[0]: r[2:4] for r in _identity(imports_db, pid)}
    assert by_sku[f"{SKU}-BLU-M"] == ("provided", "4006381333931")
    assert by_sku[f"{SKU}-RED-XL"] == ("provided", "9780306406157")
    assert by_sku[f"{SKU}-BLU-L"] == ("exempt", None)


async def test_a_saved_barcode_is_refused_with_a_reason(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    barcode = {"type": "ean", "value": "4006381333931"}
    pid = await _saved(api, imports_db, identifier_exemption=None, product_identifier=barcode)
    import_id = await _import(imports_db, api, _family_csv(exempt=False))
    results = row_results(imports_db.conn, import_id)
    assert {r[1] for r in results} == {"failed"}
    ((code, message),) = _errors(results)[f"{SKU}-BLU-M"]
    assert code == "VARIATION_INVALID"
    assert "barcode" in message
    # nothing moved: still a standalone with its barcode
    assert _identity(imports_db, pid) == [(SKU, True, "provided", "4006381333931", True)]
