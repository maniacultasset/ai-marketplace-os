"""S13b round-1 tester attacks (AC 14-21): family growth across imports, nested and conflicting
families, malformed CSV shapes, two imports of one file at once, a rows retry across tenants.
Real stack: the API as app_rw, the job wrapper and body, Postgres and the compose MinIO."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx
import pytest
from mkos_schemas.errors import ErrorReport

from .conftest import LOCAL_HOSTS
from .imports_support import (
    csv_bytes,
    headers,
    import_fixture,
    import_row,
    imports_db,
    row,
    row_results,
    run_job,
    upload,
)
from .jobs_support import JobWorld, count, job_row

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["imports_db"]


def kid(n: int, parent: str, size: str | None, colour: str | None = None) -> dict[str, str]:
    cells = {"product_name": "Tee", "parent_style_id": parent}
    if size is not None:
        cells["size_name"] = size
    if colour is not None:
        cells["color_name"] = colour
    return row(n, **cells)


def failures(conn: Any, import_id: uuid.UUID) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    for number, status, _sku, _pid, report in row_results(conn, import_id):
        if status == "failed":
            out[number] = [e.error_code.value for e in ErrorReport.model_validate(report).errors]
    return out


async def run_rows(jw: JobWorld, api: httpx.AsyncClient, data: bytes) -> tuple[uuid.UUID, str]:
    import_id, job_id = await import_fixture(jw, api, data)
    await run_job(job_id)
    return import_id, job_row(jw.conn, job_id)["status"]


def matrix(conn: Any, tenant: uuid.UUID, style: str) -> list[tuple[Any, ...]]:
    found: list[tuple[Any, ...]] = conn.execute(
        "SELECT cw.colour_key, m.size_key, m.availability, c.sku "
        "FROM variant_matrix_cells m JOIN products s ON s.id = m.style_product_id "
        "LEFT JOIN product_colourways cw ON cw.id = m.colourway_id "
        "LEFT JOIN products c ON c.id = m.variant_product_id "
        "WHERE s.sku = %s AND s.tenant_id = %s ORDER BY 1, 2",
        [style, tenant],
    ).fetchall()
    return found


async def _retry(jw: JobWorld, api: httpx.AsyncClient, import_id: uuid.UUID) -> uuid.UUID:
    got = await api.post(f"/api/v1/imports/{import_id}/rows/retry", headers=headers(jw))
    assert got.status_code == 202, got.text
    job = uuid.UUID(got.json()["job_id"])
    await run_job(job)
    return job


async def test_attack_nested_family_never_fails_the_job(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    """A style that names its own parent (A -> B -> C) is a row error, never a failed job."""
    rows = [
        row(1, seller_sku="C"),
        {**row(2, seller_sku="B"), "parent_style_id": "C", "size_name": "M"},
        kid(3, "B", "M"),
        kid(4, "B", "L"),
    ]
    import_id, status = await run_rows(imports_db, api, csv_bytes(rows))
    assert status in ("succeeded", "partial"), status
    total = count(
        imports_db.conn, "SELECT count(*) FROM import_rows WHERE import_id = %s", [import_id]
    )
    assert total == 4


async def test_attack_standalone_sku_reused_as_style_in_a_later_file(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    tenant = imports_db.tenant("acme")
    _, status = await run_rows(imports_db, api, csv_bytes([row(1, seller_sku="BASE")]))
    assert status == "succeeded"
    import_id, status = await run_rows(
        imports_db, api, csv_bytes([kid(2, "BASE", "M"), kid(3, "BASE", "L")])
    )
    assert status in ("succeeded", "partial"), status
    assert (
        count(imports_db.conn, "SELECT count(*) FROM import_rows WHERE import_id = %s", [import_id])
        == 2
    )
    assert (
        count(
            imports_db.conn,
            "SELECT count(*) FROM products WHERE tenant_id = %s AND sku = 'BASE'",
            [tenant],
        )
        == 1
    )


async def test_attack_child_sku_reimported_as_standalone(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    """A child of a family re-sent later without its parent_style_id: never a crashed job (the
    detached child is the repro `test_child_reimported_standalone_detaches_from_its_matrix`)."""
    _, status = await run_rows(imports_db, api, csv_bytes([kid(1, "TEE", "M"), kid(2, "TEE", "L")]))
    assert status == "succeeded"
    import_id, status = await run_rows(imports_db, api, csv_bytes([row(1)]))
    assert status in ("succeeded", "partial"), status
    assert (
        count(imports_db.conn, "SELECT count(*) FROM import_rows WHERE import_id = %s", [import_id])
        == 1
    )


async def test_attack_style_sku_reused_as_child_of_another_family(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    _, status = await run_rows(imports_db, api, csv_bytes([kid(1, "TEE", "M"), kid(2, "TEE", "L")]))
    assert status == "succeeded"
    import_id, status = await run_rows(
        imports_db,
        api,
        csv_bytes([{**kid(3, "OTHER", "M"), "seller_sku": "TEE"}, kid(4, "OTHER", "L")]),
    )
    assert status in ("succeeded", "partial"), status
    assert (
        count(imports_db.conn, "SELECT count(*) FROM import_rows WHERE import_id = %s", [import_id])
        == 2
    )


async def test_attack_crlf_bom_quoted_newline_file(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    data = (
        "﻿Seller_SKU , Product_Name,CATEGORY,mrp,selling_price,main_image\r\n"
        f'A1,"Kurta\r\nwith a newline",ethnic_wear_kurta_sets,"₹1,299.00",999,{row(1)["main_image"]}\r\n'
        "\r\n"
        f"A2,Kurta two,ethnic_wear_kurta_sets,1299,999,{row(2)['main_image']}\r\n"
    ).encode()
    import_id, status = await run_rows(imports_db, api, data)
    assert status == "succeeded", failures(imports_db.conn, import_id)
    numbers = [r[0] for r in row_results(imports_db.conn, import_id)]
    assert numbers == [2, 4]
    assert import_row(imports_db.conn, import_id)["imported_rows"] == 2


async def test_attack_huge_cell_and_nan_rows_fail_alone(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    rows = [
        row(1, product_name="x" * 6000),
        row(2, mrp="NaN"),
        row(3, selling_price="Infinity"),
        row(4),
    ]
    import_id, status = await run_rows(imports_db, api, csv_bytes(rows))
    assert status == "partial"
    got = failures(imports_db.conn, import_id)
    assert sorted(got) == [2, 3, 4]
    # no cell value reaches a stored report
    for *_, report in row_results(imports_db.conn, import_id):
        assert "xxxxxxxx" not in str(report) and "NaN" not in str(report)


async def test_attack_same_file_two_imports_at_once(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    """Two keys, one file, both jobs running at once: each SKU is one product, both jobs end
    with every row imported, and neither job fails."""
    tenant = imports_db.tenant("acme")
    rows = [row(n) for n in range(1, 41)] + [
        kid(41, "TEE", "M", "Navy"),
        kid(42, "TEE", "L", "Red"),
    ]
    data = csv_bytes(rows)
    a_import, a_job = await import_fixture(imports_db, api, data)
    b_import, b_job = await import_fixture(imports_db, api, data)
    await asyncio.gather(run_job(a_job), run_job(b_job))
    for job, imp in ((a_job, a_import), (b_job, b_import)):
        assert job_row(imports_db.conn, job)["status"] == "succeeded", failures(
            imports_db.conn, imp
        )
    assert (
        count(imports_db.conn, "SELECT count(*) FROM products WHERE tenant_id = %s", [tenant]) == 43
    )
    assert len(matrix(imports_db.conn, tenant, "TEE")) == 4


async def test_attack_rows_retry_other_tenant_404_and_no_job(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    import_id, status = await run_rows(
        imports_db, api, csv_bytes([row(1), row(2, category="ethnic_wear_dupattas")])
    )
    assert status == "partial"
    before = count(imports_db.conn, "SELECT count(*) FROM job_runs", [])
    got = await api.post(
        f"/api/v1/imports/{import_id}/rows/retry",
        headers=headers(imports_db, "operator@bharat.test"),
    )
    assert got.status_code == 404, got.text
    assert count(imports_db.conn, "SELECT count(*) FROM job_runs", []) == before
    rows = await api.get(
        f"/api/v1/imports/{import_id}/rows?status=failed",
        headers=headers(imports_db, "operator@bharat.test"),
    )
    assert rows.status_code == 404


async def test_attack_filename_path_and_controls(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    tenant = imports_db.tenant("acme")
    got = await upload(
        api, headers(imports_db), csv_bytes([row(1)]), filename="../../etc/‮passwd\x00.csv"
    )
    assert got.status_code == 202, got.text
    stored = import_row(imports_db.conn, uuid.UUID(got.json()["import_id"]))
    assert "/" not in stored["filename"] and "‮" not in stored["filename"]
    assert stored["storage_key"].startswith(f"{tenant}/imports/")


async def test_attack_theme_change_is_a_row_error(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    """A size-only family that gets a colour in a later file: the new child is a row error,
    the stored family and its matrix are untouched."""
    tenant = imports_db.tenant("acme")
    _, status = await run_rows(imports_db, api, csv_bytes([kid(1, "TEE", "M"), kid(2, "TEE", "L")]))
    assert status == "succeeded"
    imp, status = await run_rows(imports_db, api, csv_bytes([kid(3, "TEE", "M", "Navy")]))
    assert status == "partial"
    assert failures(imports_db.conn, imp) == {2: ["VARIATION_INVALID"]}
    assert matrix(imports_db.conn, tenant, "TEE") == [
        (None, "l", "available", "SKU-00002"),
        (None, "m", "available", "SKU-00001"),
    ]


async def test_attack_rows_retry_of_a_whole_unmapped_family(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    from .imports_support import UNMAPPED, seed_templates

    tenant = imports_db.tenant("acme")
    fam = [
        {**kid(1, "FAM", "M", "Navy"), "category": UNMAPPED},
        {**kid(2, "FAM", "L", "Red"), "category": UNMAPPED},
        row(3),
    ]
    import_id, status = await run_rows(imports_db, api, csv_bytes(fam))
    assert status == "partial"
    assert sorted(failures(imports_db.conn, import_id)) == [2, 3]
    seed_templates(imports_db.conn, [UNMAPPED])
    job = await _retry(imports_db, api, import_id)
    assert job_row(imports_db.conn, job)["status"] == "succeeded", failures(
        imports_db.conn, import_id
    )
    assert import_row(imports_db.conn, import_id)["failed_rows"] == 0
    assert len(matrix(imports_db.conn, tenant, "FAM")) == 4
