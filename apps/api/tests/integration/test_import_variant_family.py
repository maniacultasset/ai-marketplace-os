"""S13 B10 (AC 17; DATABASE.md:971-974, :452; ADR-0007, T17): families through the real job
body, as app_rw. A family is one unit (Q12): its style and colourways in pass 1, children and
their cells in pass 2, then one derived `not_provided` cell per missing colour x size."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from app.services.imports import runner
from mkos_schemas.errors import ErrorReport

from .conftest import LOCAL_HOSTS
from .imports_support import (
    csv_bytes,
    import_fixture,
    import_row,
    imports_db,
    one,
    row,
    row_results,
    run_job,
)
from .jobs_support import JobWorld, count, job_row

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["imports_db"]


def child(
    n: int, parent: str, size: str | None, colour: str | None, **cells: str
) -> dict[str, str]:
    extra: dict[str, str] = {
        "product_name": "Cotton Tee",
        "parent_style_id": parent,
        "brand_name": "Acme",
        **cells,
    }
    if size is not None:
        extra["size_name"] = size
    if colour is not None:
        extra["color_name"] = colour
    return row(n, **extra)


def failed(conn: Any, import_id: uuid.UUID) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    for number, status, _sku, _pid, report in row_results(conn, import_id):
        if status == "failed":
            out[number] = [e.error_code.value for e in ErrorReport.model_validate(report).errors]
    return out


def cells(conn: Any, style_sku: str, tenant: uuid.UUID) -> list[tuple[Any, ...]]:
    found: list[tuple[Any, ...]] = conn.execute(
        "SELECT cw.colour_key, m.size_key, m.availability, m.unavailable_reason, m.source, "
        "c.sku FROM variant_matrix_cells m JOIN products s ON s.id = m.style_product_id "
        "LEFT JOIN product_colourways cw ON cw.id = m.colourway_id "
        "LEFT JOIN products c ON c.id = m.variant_product_id "
        "WHERE s.sku = %s AND s.tenant_id = %s ORDER BY 1, 2",
        [style_sku, tenant],
    ).fetchall()
    return found


async def run(jw: JobWorld, api: httpx.AsyncClient, rows: list[dict[str, str]]) -> uuid.UUID:
    import_id, job_id = await import_fixture(jw, api, csv_bytes(rows))
    await run_job(job_id)
    assert job_row(jw.conn, job_id)["status"] in ("succeeded", "partial")
    return import_id


async def test_import_variant_family_parent_sku_links(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    tenant = imports_db.tenant("acme")
    rows = [
        child(1, "TEE", "M", "Navy"),
        child(2, "TEE", "L", "Navy"),
        {**child(3, "", "M", "Jet Black"), "parent_sku": "TEE"},  # the ADR-0007 alias header
    ]
    for r in rows[:2]:
        r["parent_sku"] = r.pop("parent_style_id")
    rows[2].pop("parent_style_id")
    import_id = await run(imports_db, api, rows)
    assert failed(imports_db.conn, import_id) == {}
    style = imports_db.conn.execute(
        "SELECT id, variation_theme, size_run, brand_name, parent_product_id FROM products "
        "WHERE tenant_id = %s AND sku = 'TEE'",
        [tenant],
    ).fetchone()
    assert style is not None and style[1:] == ("size_color", ["m", "l"], "Acme", None)
    children = imports_db.conn.execute(
        "SELECT sku, parent_product_id, variant_key, brand_name FROM products "
        "WHERE tenant_id = %s AND parent_product_id IS NOT NULL ORDER BY sku",
        [tenant],
    ).fetchall()
    assert children == [
        ("SKU-00001", style[0], "color=navy|size=m", None),
        ("SKU-00002", style[0], "color=navy|size=l", None),
        ("SKU-00003", style[0], "color=jet black|size=m", None),
    ]
    colours = imports_db.conn.execute(
        "SELECT colour_key, attributes -> 'color' ->> 'value' FROM product_colourways "
        "WHERE style_product_id = %s ORDER BY position",
        [style[0]],
    ).fetchall()
    assert colours == [("navy", "Navy"), ("jet black", "Jet Black")]
    assert import_row(imports_db.conn, import_id)["imported_rows"] == 3


async def test_import_variant_family_style_value_disagrees(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    import_id = await run(
        imports_db,
        api,
        [
            child(1, "TEE", "M", None),
            child(2, "TEE", "L", None, brand_name="Other Brand"),
            child(3, "TEE", "XL", None, gender="men"),
            child(4, "TEE", "S", None, product_name="Another Tee"),
        ],
    )
    assert failed(imports_db.conn, import_id) == {
        3: ["CROSS_FIELD_MISMATCH"],
        4: ["CROSS_FIELD_MISMATCH"],
        5: ["CROSS_FIELD_MISMATCH"],
    }
    run_sizes = one(
        imports_db.conn,
        "SELECT size_run FROM products WHERE sku = 'TEE' AND tenant_id = %s",
        [imports_db.tenant("acme")],
    )
    assert run_sizes == ["m"]


async def test_import_variant_family_size_colour_casefold_collides(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    import_id = await run(
        imports_db, api, [child(1, "TEE", "M", "Navy"), child(2, "TEE", "m", "navy ")]
    )
    assert failed(imports_db.conn, import_id) == {3: ["VARIATION_INVALID"]}


async def test_import_variant_family_matrix_complete(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    tenant = imports_db.tenant("acme")
    await run(
        imports_db,
        api,
        [child(1, "TEE", "M", "Navy"), child(2, "TEE", "L", "Navy"), child(3, "TEE", "M", "Red")],
    )
    assert cells(imports_db.conn, "TEE", tenant) == [
        ("navy", "l", "available", None, "seller", "SKU-00002"),
        ("navy", "m", "available", None, "seller", "SKU-00001"),
        ("red", "l", "unavailable", "not_provided", "derived", None),
        ("red", "m", "available", None, "seller", "SKU-00003"),
    ]
    evidence = one(
        imports_db.conn,
        "SELECT evidence_ref FROM variant_matrix_cells WHERE source = 'derived'",
    )
    assert evidence.startswith("import:")


async def test_import_variant_family_parent_other_tenant_not_linked(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    from .imports_support import import_fixture as upload_as

    bharat = imports_db.tenant("bharat")
    _, job = await upload_as(
        imports_db, api, csv_bytes([child(1, "TEE", "M", None)]), "operator@bharat.test"
    )
    await run_job(job)
    theirs = one(
        imports_db.conn, "SELECT id FROM products WHERE tenant_id = %s AND sku = 'TEE'", [bharat]
    )
    await run(imports_db, api, [child(5, "TEE", "S", None)])
    acme = imports_db.tenant("acme")
    ours = one(
        imports_db.conn, "SELECT id FROM products WHERE tenant_id = %s AND sku = 'TEE'", [acme]
    )
    assert ours != theirs
    parent = one(
        imports_db.conn,
        "SELECT parent_product_id FROM products WHERE tenant_id = %s AND sku = 'SKU-00005'",
        [acme],
    )
    assert parent == ours
    assert one(imports_db.conn, "SELECT size_run FROM products WHERE id = %s", [theirs]) == ["m"]


async def test_import_variant_family_never_split_across_commits(
    imports_db: JobWorld, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    batches: list[list[int]] = []
    real = runner._run_batch

    async def spy(run_: Any, batch: list[runner.Unit]) -> None:
        batches.append([len(u.rows) for u in batch])
        await real(run_, batch)

    monkeypatch.setattr(runner, "_run_batch", spy)
    family = [child(n, "BIG", None, f"Colour {n}") for n in range(1, 151)]
    singles = [row(n) for n in range(200, 260)]
    await run(imports_db, api, [*singles[:30], *family, *singles[30:]])
    assert [150] in batches
    assert all(sum(b) <= 100 or b == [150] for b in batches)
    tenant = imports_db.tenant("acme")
    assert (
        count(
            imports_db.conn,
            "SELECT count(*) FROM products WHERE tenant_id = %s AND parent_product_id IS NOT NULL",
            [tenant],
        )
        == 150
    )


async def test_import_family_rows_non_contiguous(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    """Children at rows 2 and 900, the style row between: one unit, a complete matrix."""
    middle = [row(n) for n in range(3, 899)]
    rows = [
        child(1, "SCAT", "M", "Navy"),
        *middle[:400],
        row(5000, seller_sku="SCAT", product_name="Cotton Tee", brand_name="Acme"),
        *middle[400:],
        child(2, "SCAT", "L", "Red"),
    ]
    assert len(rows) == 899
    import_id = await run(imports_db, api, rows)
    assert failed(imports_db.conn, import_id) == {}
    imp = import_row(imports_db.conn, import_id)
    assert (imp["total_rows"], imp["imported_rows"], imp["failed_rows"]) == (899, 899, 0)
    tenant = imports_db.tenant("acme")
    assert cells(imports_db.conn, "SCAT", tenant) == [
        ("navy", "l", "unavailable", "not_provided", "derived", None),
        ("navy", "m", "available", None, "seller", "SKU-00001"),
        ("red", "l", "available", None, "seller", "SKU-00002"),
        ("red", "m", "unavailable", "not_provided", "derived", None),
    ]
    style_row = one(
        imports_db.conn,
        "SELECT r.product_id = p.id FROM import_rows r JOIN products p ON p.sku = 'SCAT' "
        "AND p.tenant_id = r.tenant_id WHERE r.import_id = %s AND r.sku = 'SCAT'",
        [import_id],
    )
    assert style_row is True


async def test_import_variant_family_child_db_error_matrix_still_valid(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    gtin = "4006381333931"
    import_id = await run(
        imports_db,
        api,
        [
            child(1, "TEE", "M", "Navy", product_identifier_type="ean", product_identifier=gtin),
            child(2, "TEE", "L", "Navy", product_identifier_type="ean", product_identifier=gtin),
            child(3, "TEE", "M", "Red"),
        ],
    )
    assert failed(imports_db.conn, import_id) == {3: ["IDENTIFIER_INVALID"]}
    tenant = imports_db.tenant("acme")
    assert cells(imports_db.conn, "TEE", tenant) == [
        ("navy", "l", "unavailable", "not_provided", "derived", None),
        ("navy", "m", "available", None, "seller", "SKU-00001"),
        ("red", "l", "unavailable", "not_provided", "derived", None),
        ("red", "m", "available", None, "seller", "SKU-00003"),
    ]
    orphans = count(
        imports_db.conn,
        "SELECT count(*) FROM products WHERE tenant_id = %s AND sku = 'SKU-00002'",
        [tenant],
    )
    assert orphans == 0


async def test_import_variant_family_grows(imports_db: JobWorld, api: httpx.AsyncClient) -> None:
    """A family that grows in a later file keeps a complete matrix: the stored sizes get a gap
    cell for the new colour, and the new size one for the stored colour (round-1 F1)."""
    tenant = imports_db.tenant("acme")
    first = await run(imports_db, api, [child(1, "TEE", "M", "Navy"), child(2, "TEE", "L", "Navy")])
    assert failed(imports_db.conn, first) == {}
    second = await run(
        imports_db, api, [child(3, "TEE", "XL", "Navy"), child(4, "TEE", "M", "Red")]
    )
    assert failed(imports_db.conn, second) == {}
    assert one(
        imports_db.conn,
        "SELECT size_run FROM products WHERE sku = 'TEE' AND tenant_id = %s",
        [tenant],
    ) == ["m", "l", "xl"]
    assert cells(imports_db.conn, "TEE", tenant) == [
        ("navy", "l", "available", None, "seller", "SKU-00002"),
        ("navy", "m", "available", None, "seller", "SKU-00001"),
        ("navy", "xl", "available", None, "seller", "SKU-00003"),
        ("red", "l", "unavailable", "not_provided", "derived", None),
        ("red", "m", "available", None, "seller", "SKU-00004"),
        ("red", "xl", "unavailable", "not_provided", "derived", None),
    ]


@pytest.mark.parametrize(
    ("sizes", "colours", "theme", "size_run"),
    [
        (("M", "L"), ("Red", "Blue"), "size_color", ["m", "l"]),
        (("M", "L"), (None, None), "size", ["m", "l"]),
        ((None, None), ("Red", "Blue"), "color", None),
    ],
)
async def test_import_style_resent_alone_is_a_row_error(
    imports_db: JobWorld,
    api: httpx.AsyncClient,
    sizes: tuple[str | None, str | None],
    colours: tuple[str | None, str | None],
    theme: str,
    size_run: list[str] | None,
) -> None:
    """A stored style sent again as a plain row (sku = the style key, no children in the file)
    fails with CROSS_FIELD_MISMATCH; its variation_theme, size_run and cells stay (round-2 F5)."""
    tenant = imports_db.tenant("acme")
    style = row(1, seller_sku="TEE", product_name="Cotton Tee", brand_name="Acme")
    await run(
        imports_db,
        api,
        [style, child(2, "TEE", sizes[0], colours[0]), child(3, "TEE", sizes[1], colours[1])],
    )
    query = (
        "SELECT title, variation_theme, size_run, updated_at FROM products "
        "WHERE tenant_id = %s AND sku = 'TEE'"
    )
    stored = imports_db.conn.execute(query, [tenant]).fetchone()
    assert stored is not None and stored[1] == theme and stored[2] == size_run
    before = cells(imports_db.conn, "TEE", tenant)
    import_id = await run(imports_db, api, [{**style, "product_name": "Cotton Tee v2"}])
    assert failed(imports_db.conn, import_id) == {2: ["CROSS_FIELD_MISMATCH"]}
    report = one(
        imports_db.conn,
        "SELECT error_report_json FROM import_rows WHERE import_id = %s AND row_number = 2",
        [import_id],
    )
    assert "parent_style_id" in ErrorReport.model_validate(report).errors[0].message
    assert imports_db.conn.execute(query, [tenant]).fetchone() == stored
    assert cells(imports_db.conn, "TEE", tenant) == before


async def test_import_child_resent_standalone_is_a_row_error(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    """A family's child sent again without parent_style_id fails its row with
    CROSS_FIELD_MISMATCH and stays attached to its matrix cell (round-1 F2)."""
    tenant = imports_db.tenant("acme")
    await run(imports_db, api, [child(1, "TEE", "M", None), child(2, "TEE", "L", None)])
    stored = imports_db.conn.execute(
        "SELECT parent_product_id, variant_attributes, updated_at FROM products "
        "WHERE tenant_id = %s AND sku = 'SKU-00001'",
        [tenant],
    ).fetchone()
    import_id = await run(imports_db, api, [row(1), row(2, seller_sku="PLAIN-1")])
    assert failed(imports_db.conn, import_id) == {2: ["CROSS_FIELD_MISMATCH"]}
    report = one(
        imports_db.conn,
        "SELECT error_report_json FROM import_rows WHERE import_id = %s AND row_number = 2",
        [import_id],
    )
    message = ErrorReport.model_validate(report).errors[0].message
    assert "parent_style_id" in message and "SKU-00001" not in message
    after = imports_db.conn.execute(
        "SELECT parent_product_id, variant_attributes, updated_at FROM products "
        "WHERE tenant_id = %s AND sku = 'SKU-00001'",
        [tenant],
    ).fetchone()
    assert after == stored
    assert ("m", "available", "SKU-00001") in [
        (c[1], c[2], c[5]) for c in cells(imports_db.conn, "TEE", tenant)
    ]
