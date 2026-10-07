"""S13 B9 repositories (R4: the import's SQL lives here), as app_rw: the product upsert on
`(tenant_id, sku)`, colourway and matrix-cell upserts, party match-or-create on the dedupe key,
and the `import_rows` writes and totals."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from app.db.guard import CrossTenantQueryError
from app.db.session import unit_of_work
from app.repositories.imports import ImportRepository, ImportRowRepository, RowWrite
from app.repositories.parties import BusinessPartyRepository, PartyFields
from app.repositories.products import ProductRepository, ProductUpsert
from app.repositories.variants import ColourwayRepository, MatrixCellRepository
from mkos_schemas.enums import JobKind

from .conftest import LOCAL_HOSTS
from .imports_support import imports_db
from .jobs_support import JobWorld, count

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["imports_db"]
STAMP = {"source": "seller", "captured_via": "import", "updated_at": "2026-10-07T00:00:00Z"}


def upsert(sku: str = "UP-1", **extra: Any) -> ProductUpsert:
    return ProductUpsert(sku=sku, title="Kurta", category="kurtas", stamp=STAMP, **extra)


async def test_upsert_by_sku_insert_update_revive(imports_db: JobWorld) -> None:
    tenant = imports_db.tenant("acme")
    async with unit_of_work(tenant) as session:
        first = await ProductRepository(session, tenant).upsert_by_sku(upsert(brand_name="Acme"))
    assert first.inserted
    stamp = imports_db.conn.execute(
        "SELECT updated_at FROM products WHERE id = %s", [first.id]
    ).fetchone()
    async with unit_of_work(tenant) as session:
        same = await ProductRepository(session, tenant).upsert_by_sku(upsert())
    assert (same.id, same.inserted) == (first.id, False)
    row = imports_db.conn.execute(
        "SELECT updated_at, brand_name FROM products WHERE id = %s", [first.id]
    ).fetchone()
    assert row == (stamp[0] if stamp else None, "Acme")  # unchanged: not rewritten; blank kept
    async with unit_of_work(tenant) as session:
        renamed = await ProductRepository(session, tenant).upsert_by_sku(
            ProductUpsert(sku="UP-1", title="Kurta Set", category="kurtas", stamp=STAMP)
        )
    assert renamed.id == first.id
    imports_db.conn.execute("UPDATE products SET deleted_at = now() WHERE id = %s", [first.id])
    async with unit_of_work(tenant) as session:
        revived = await ProductRepository(session, tenant).upsert_by_sku(upsert())
    assert revived.id == first.id
    found = imports_db.conn.execute(
        "SELECT title, deleted_at, provenance ? 'title', provenance ? 'brand_name' "
        "FROM products WHERE id = %s",
        [first.id],
    ).fetchone()
    assert found == ("Kurta", None, True, True)


async def test_upsert_by_sku_parent_link(imports_db: JobWorld) -> None:
    tenant = imports_db.tenant("acme")
    async with unit_of_work(tenant) as session:
        repo = ProductRepository(session, tenant)
        style = await repo.upsert_by_sku(
            upsert("STYLE-1", variation_theme="size", size_run=("s", "m"))
        )
        child = await repo.upsert_by_sku(
            upsert("STYLE-1-S", variant_attributes={"size": "s"}), parent_product_id=style.id
        )
        await repo.upsert_by_sku(upsert("STYLE-1", variation_theme="size", size_run=("l", "m")))
        cells = MatrixCellRepository(session, tenant)
        from app.repositories.facts import FactWriter

        await FactWriter(session, tenant).set_attribute(
            child.id, "size", {"value": "S", "normalized": "s", **STAMP}
        )
        await cells.upsert(style.id, None, "s", child.id)
    row = imports_db.conn.execute(
        "SELECT parent_product_id, variant_key FROM products WHERE id = %s", [child.id]
    ).fetchone()
    assert row == (style.id, "size=s")
    run = imports_db.conn.execute(
        "SELECT size_run FROM products WHERE id = %s", [style.id]
    ).fetchone()
    assert run == (["s", "m", "l"],)


async def test_upsert_by_sku_other_tenant_rowcount_error(imports_db: JobWorld) -> None:
    acme, bharat = imports_db.tenant("acme"), imports_db.tenant("bharat")
    with pytest.raises(CrossTenantQueryError):
        async with unit_of_work(bharat) as session:
            await ProductRepository(session, acme).upsert_by_sku(upsert())
    assert count(imports_db.conn, "SELECT count(*) FROM products") == 0


async def _style(tenant: uuid.UUID, theme: str = "size_color") -> uuid.UUID:
    async with unit_of_work(tenant) as session:
        done = await ProductRepository(session, tenant).upsert_by_sku(
            upsert("ST-9", variation_theme=theme, size_run=("m",))
        )
    return done.id


async def test_colourway_upsert_idempotent(imports_db: JobWorld) -> None:
    tenant = imports_db.tenant("acme")
    style = await _style(tenant)
    fact = {"value": "Jet Black", "normalized": "black", **STAMP}
    ids = []
    for _ in range(2):
        async with unit_of_work(tenant) as session:
            ids.append(await ColourwayRepository(session, tenant).upsert(style, fact, 1))
    async with unit_of_work(tenant) as session:
        ids.append(
            await ColourwayRepository(session, tenant).upsert(
                style, {**fact, "value": " jet black "}, 1
            )
        )
    assert ids[0] == ids[1] == ids[2]
    rows = imports_db.conn.execute(
        "SELECT colour_key, attributes -> 'color' ->> 'value' FROM product_colourways"
    ).fetchall()
    assert rows == [("jet black", " jet black ")]


@pytest.mark.parametrize("value", ["İndigo", "ÉCRU", "Ölive Grün"])
async def test_colourway_unchanged_reupload_finds_the_row(imports_db: JobWorld, value: str) -> None:
    # An unchanged re-upload takes the lookup path; Python's lower() differs from PG's
    # lower(btrim()) (colour_key) for these, so the key is computed in SQL (S13b review #5).
    tenant = imports_db.tenant("acme")
    style = await _style(tenant)
    fact = {"value": value, "normalized": "blue", **STAMP}
    ids = []
    for _ in range(2):
        async with unit_of_work(tenant) as session:
            ids.append(await ColourwayRepository(session, tenant).upsert(style, fact, 1))
    assert ids[0] == ids[1]


async def test_matrix_cell_upsert_idempotent(imports_db: JobWorld) -> None:
    tenant = imports_db.tenant("acme")
    style = await _style(tenant)
    async with unit_of_work(tenant) as session:
        colourway = await ColourwayRepository(session, tenant).upsert(
            style, {"value": "Navy", **STAMP}, 1
        )
        cells = MatrixCellRepository(session, tenant)
        assert await cells.fill_gap(style, colourway, "m", "import:x")
        assert not await cells.fill_gap(style, colourway, "m", "import:y")
    rows = imports_db.conn.execute(
        "SELECT availability, unavailable_reason, source, evidence_ref FROM variant_matrix_cells"
    ).fetchall()
    assert rows == [("unavailable", "not_provided", "derived", "import:x")]


async def test_party_match_or_create_dedupe(imports_db: JobWorld) -> None:
    tenant = imports_db.tenant("acme")
    async with unit_of_work(tenant) as session:
        parties = BusinessPartyRepository(session, tenant)
        a = await parties.match_or_create(PartyFields("manufacturer", legal_name="Acme Mills"))
        b = await parties.match_or_create(PartyFields("manufacturer", legal_name="  acme mills "))
        c = await parties.match_or_create(PartyFields("packer", legal_name="Acme Mills"))
        d = await parties.match_or_create(PartyFields("consumer_care", email="Care@Acme.test"))
        e = await parties.match_or_create(PartyFields("consumer_care", email="care@acme.test"))
    assert a == b == c and d == e and a != d
    roles = imports_db.conn.execute(
        "SELECT roles FROM business_parties WHERE id = %s", [a]
    ).fetchone()
    assert roles == (["manufacturer", "packer"],)


async def _import(jw: JobWorld) -> tuple[uuid.UUID, uuid.UUID]:
    from app.repositories.jobs import EnqueueSpec, JobRunRepository

    tenant = jw.tenant("acme")
    import_id = uuid.uuid4()
    async with unit_of_work(tenant) as session:
        job = await JobRunRepository(session, tenant).enqueue(
            EnqueueSpec(JobKind.BULK_IMPORT, "import", import_id, jw.owner("acme"))
        )
        await ImportRepository(session, tenant).create(
            import_id=import_id,
            storage_key=f"{tenant}/imports/{import_id}.csv",
            filename="x.csv",
            job_id=job.job.id,
            created_by=jw.owner("acme"),
        )
    return import_id, job.job.id


async def test_import_row_write_idempotent_on_row_number(imports_db: JobWorld) -> None:
    tenant = imports_db.tenant("acme")
    import_id, job_id = await _import(imports_db)
    for status in ("failed", "imported", "imported"):
        async with unit_of_work(tenant) as session:
            await ImportRowRepository(session, tenant).write(
                import_id, job_id, RowWrite(2, status, {"seller_sku": "A"}, sku="A")
            )
    rows = imports_db.conn.execute(
        "SELECT row_number, status FROM import_rows WHERE import_id = %s", [import_id]
    ).fetchall()
    assert rows == [(2, "imported")]
    with pytest.raises(ValueError, match="imported or failed"):
        async with unit_of_work(tenant) as session:
            await ImportRowRepository(session, tenant).write(
                import_id, job_id, RowWrite(3, "pending", {})
            )


async def test_import_row_totals(imports_db: JobWorld) -> None:
    tenant = imports_db.tenant("acme")
    import_id, job_id = await _import(imports_db)
    async with unit_of_work(tenant) as session:
        repo = ImportRowRepository(session, tenant)
        for number, status in ((2, "imported"), (3, "failed"), (4, "imported")):
            await repo.write(import_id, job_id, RowWrite(number, status, {}))
        totals = await repo.totals(import_id)
        assert (totals.total, totals.imported, totals.failed) == (3, 2, 1)
        assert await repo.finished_row_numbers(import_id) == {2, 3, 4}
        page, more = await repo.page(import_id, status="imported", after=None, limit=1)
        assert [r.row_number for r in page] == [2] and more
