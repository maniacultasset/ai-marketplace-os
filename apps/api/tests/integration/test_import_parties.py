"""S13 B11 (AC 14; DATABASE.md:973-974, :2190, :2194): the import's parties, as app_rw. The
manufacturer is matched on its dedupe key with the address kept whole; consumer care equal to
the brand default is inherited, any other contact is a nameless consumer-care party."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from app.services.imports.runner import same_contact, unambiguous_pin

from .conftest import LOCAL_HOSTS
from .imports_support import csv_bytes, import_fixture, imports_db, one, row, run_job
from .jobs_support import JobWorld, job_row

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["imports_db"]


async def run(jw: JobWorld, api: httpx.AsyncClient, rows: list[dict[str, str]]) -> None:
    _, job_id = await import_fixture(jw, api, csv_bytes(rows))
    await run_job(job_id)
    assert job_row(jw.conn, job_id)["status"] == "succeeded", job_row(jw.conn, job_id)[
        "error_report_json"
    ]


def party_of(conn: Any, tenant: uuid.UUID, sku: str, column: str) -> Any:
    return one(
        conn,
        f"SELECT {column} FROM products WHERE tenant_id = %s AND sku = %s",  # noqa: S608
        [tenant, sku],
    )


async def test_import_parties_manufacturer_dedupe(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    tenant = imports_db.tenant("acme")
    address = "12 MG Road, Bengaluru 560001"
    await run(
        imports_db,
        api,
        [
            row(1, manufacturer_name="Acme Mills", manufacturer_address=address),
            row(2, manufacturer_name="  ACME mills ", manufacturer_address=address),
            row(3, manufacturer_name="Acme Mills"),
        ],
    )
    first = party_of(imports_db.conn, tenant, "SKU-00001", "manufacturer_party_id")
    assert party_of(imports_db.conn, tenant, "SKU-00002", "manufacturer_party_id") == first
    third = party_of(imports_db.conn, tenant, "SKU-00003", "manufacturer_party_id")
    assert third != first  # no address: another dedupe key
    stored = imports_db.conn.execute(
        "SELECT legal_name, address_line1, postal_code, roles, source FROM business_parties "
        "WHERE id = %s",
        [first],
    ).fetchone()
    assert stored == ("Acme Mills", address, "560001", ["manufacturer"], "seller")
    provenance = party_of(imports_db.conn, tenant, "SKU-00001", "provenance -> 'manufacturer'")
    assert provenance["value"] == address and provenance["captured_via"] == "import"


@pytest.mark.parametrize(
    ("address", "pin"),
    [
        ("12 MG Road, Bengaluru 560001", "560001"),
        ("12 MG Road, Bengaluru - 560001.", "560001"),
        ("Plot 110001, Sector 5, Noida 201301", None),
        ("560001 MG Road, Bengaluru", None),
        ("Mill Road, Surat", None),
        ("Unit 1234567, Surat", None),
        (None, None),
    ],
)
def test_import_parties_address_pin_unambiguous_only(address: str | None, pin: str | None) -> None:
    assert unambiguous_pin(address) == pin


async def test_import_parties_address_kept_whole(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    address = "Plot 110001, Sector 5, Noida 201301"
    await run(
        imports_db, api, [row(1, manufacturer_name="Noida Works", manufacturer_address=address)]
    )
    stored = imports_db.conn.execute(
        "SELECT address_line1, postal_code, city, state_code FROM business_parties"
    ).fetchone()
    assert stored == (address, None, None, None)


async def _brand_care(jw: JobWorld, email: str, phone: str | None) -> uuid.UUID:
    tenant = jw.tenant("acme")
    party = one(
        jw.conn,
        "INSERT INTO business_parties (tenant_id, roles, email, phone, source) "
        "VALUES (%s, '{consumer_care}', %s, %s, 'seller') RETURNING id",
        [tenant, email, phone],
    )
    jw.conn.execute(
        "INSERT INTO brand_profiles (tenant_id, consumer_care_party_id, provenance) "
        "VALUES (%s, %s, '{}') ON CONFLICT (tenant_id) DO UPDATE "
        "SET consumer_care_party_id = EXCLUDED.consumer_care_party_id",
        [tenant, party],
    )
    return party  # type: ignore[no-any-return]


async def test_import_parties_consumer_care_brand_default(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    tenant = imports_db.tenant("acme")
    await _brand_care(imports_db, "care@acme.test", "+91 98765 43210")
    await run(
        imports_db,
        api,
        [
            row(1, consumer_care_email="CARE@acme.test"),
            row(2, consumer_care_phone="+91-9876543210"),
            row(3, consumer_care_email="care@acme.test", consumer_care_phone="+91 98765 43210"),
        ],
    )
    for sku in ("SKU-00001", "SKU-00002", "SKU-00003"):
        assert party_of(imports_db.conn, tenant, sku, "consumer_care_party_id") is None
    assert one(imports_db.conn, "SELECT count(*) FROM business_parties") == 1


async def test_import_parties_consumer_care_nameless(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    tenant = imports_db.tenant("acme")
    brand = await _brand_care(imports_db, "care@acme.test", None)
    await run(
        imports_db,
        api,
        [
            row(1, consumer_care_email="help@acme.test"),
            row(2, consumer_care_email="HELP@acme.test"),
            row(3, consumer_care_phone="+91 99999 00000"),
        ],
    )
    first = party_of(imports_db.conn, tenant, "SKU-00001", "consumer_care_party_id")
    assert first is not None and first != brand
    assert party_of(imports_db.conn, tenant, "SKU-00002", "consumer_care_party_id") == first
    stored = imports_db.conn.execute(
        "SELECT legal_name, roles, email FROM business_parties WHERE id = %s", [first]
    ).fetchone()
    assert stored == (None, ["consumer_care"], "help@acme.test")
    phone_party = party_of(imports_db.conn, tenant, "SKU-00003", "consumer_care_party_id")
    assert phone_party not in (None, first, brand)


async def test_import_parties_no_invented_name(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    await run(
        imports_db,
        api,
        [
            row(1, consumer_care_email="help@acme.test"),
            row(2, manufacturer_name="Real Mills", consumer_care_phone="+91 99999 00000"),
        ],
    )
    names = {
        r[0] for r in imports_db.conn.execute("SELECT legal_name FROM business_parties").fetchall()
    }
    assert names == {None, "Real Mills"}


def test_same_contact_rules() -> None:
    assert same_contact("a@x.test", "+91 1", "A@X.test", None)
    assert same_contact("a@x.test", "+91-98765", None, "9198765")
    assert not same_contact("a@x.test", None, "b@x.test", None)
    assert not same_contact(None, None, None, "123456")
