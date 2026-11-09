"""SI14 T18 (AC8): `POST /products/bulk` with a confirmed `column_map_id`: the seller's own header
is renamed by the map at the upload and in the import job (first run and rows retry), the stored
file and each row's `raw_row` keep the seller's keys, and without the field nothing changes."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from .assist_support import set_switch
from .conftest import LOCAL_HOSTS
from .imports_support import (
    BULK_URL,
    MAPPED,
    csv_bytes,
    headers,
    import_row,
    imports_db,
    one,
    run_job,
)
from .jobs_support import JobWorld, as_member, job_row

__all__ = ["imports_db"]
pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
MAPS = "/api/v1/imports/column-maps"
SELLER = ["Item SKU", "Product Title", "Category", "Colour", "Brand", "Internal Notes"]


@pytest.fixture(autouse=True)
def _ai_off(monkeypatch: pytest.MonkeyPatch) -> None:
    set_switch(monkeypatch, "columns", on=False)


async def _confirmed(api: httpx.AsyncClient, jw: JobWorld, columns: list[str] = SELLER) -> str:
    made = await api.post(MAPS, headers=headers(jw), json={"headers": columns})
    assert made.status_code == 201, made.text
    body = made.json()
    answers = [
        {"position": m["position"], "key": None} for m in body["mappings"] if m["needs_review"]
    ]
    confirm = await api.post(
        f"{MAPS}/{body['id']}/confirm",
        headers=headers(jw),
        json={"mappings": answers or [{"position": 1, "key": body["mappings"][0]["key"]}]},
    )
    assert confirm.status_code == 200, confirm.text
    return str(body["id"])


def _sheet(*rows: dict[str, str]) -> bytes:
    return csv_bytes(rows, header=SELLER)


def _seller_row(n: int, brand: str = "Acme Apparel") -> dict[str, str]:
    return {
        "Item SKU": f"MAP-{n:04d}",
        "Product Title": f"Indigo Kurta {n}",
        "Category": MAPPED[0],
        "Colour": "Indigo",
        "Brand": brand,
        "Internal Notes": "do not import this",
    }


async def _upload(
    api: httpx.AsyncClient,
    jw: JobWorld,
    data: bytes,
    map_id: str | None,
    *,
    key: str | None = None,
) -> httpx.Response:
    form = {} if map_id is None else {"column_map_id": map_id}
    return await api.post(
        BULK_URL,
        headers=headers(jw, key=key),
        files={"file": ("products.csv", data, "text/csv")},
        data=form,
    )


def _code(response: httpx.Response) -> tuple[str, list[str]]:
    error = response.json()["error"]
    return error["type"], [e["field"] for e in error["error_report"]["errors"]]


def _product(jw: JobWorld, sku: str) -> dict[str, Any] | None:
    cur = jw.conn.execute(
        "SELECT sku, title, category, attributes FROM products WHERE tenant_id = %s AND sku = %s",
        [jw.tenant("acme"), sku],
    )
    names = [d.name for d in cur.description or ()]
    found = cur.fetchone()
    return None if found is None else dict(zip(names, found, strict=True))


async def test_rows_import_under_the_mapped_keys(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    map_id = await _confirmed(api, imports_db)
    response = await _upload(api, imports_db, _sheet(_seller_row(1), _seller_row(2)), map_id)
    assert response.status_code == 202, response.text
    import_id = uuid.UUID(response.json()["import_id"])
    assert str(import_row(imports_db.conn, import_id)["column_map_id"]) == map_id
    await run_job(uuid.UUID(response.json()["job_id"]))
    assert job_row(imports_db.conn, uuid.UUID(response.json()["job_id"]))["status"] == "succeeded"
    product = _product(imports_db, "MAP-0001")
    assert product is not None
    assert (product["title"], product["category"]) == ("Indigo Kurta 1", MAPPED[0])
    assert "Indigo" in str(product["attributes"])
    assert "do not import" not in str(product["attributes"])
    raw = one(
        imports_db.conn,
        "SELECT raw_row FROM import_rows WHERE import_id = %s ORDER BY row_number LIMIT 1",
        [import_id],
    )
    assert set(raw) == {s.lower() for s in SELLER}  # the seller's own keys


async def test_a_retried_row_is_renamed_again(imports_db: JobWorld, api: httpx.AsyncClient) -> None:
    map_id = await _confirmed(api, imports_db)
    data = _sheet(_seller_row(1), _seller_row(2, brand="Brand Not Yet Listed"))
    response = await _upload(api, imports_db, data, map_id)
    import_id = uuid.UUID(response.json()["import_id"])
    first = uuid.UUID(response.json()["job_id"])
    await run_job(first)
    assert job_row(imports_db.conn, first)["status"] == "partial"
    assert _product(imports_db, "MAP-0002") is None
    imports_db.conn.execute(
        "UPDATE brand_profiles SET brand_names = array_append(brand_names, 'Brand Not Yet Listed') "
        "WHERE tenant_id = %s",
        [imports_db.tenant("acme")],
    )
    retried = await api.post(f"/api/v1/imports/{import_id}/rows/retry", headers=headers(imports_db))
    assert retried.status_code == 202, retried.text
    await run_job(uuid.UUID(retried.json()["job_id"]))
    product = _product(imports_db, "MAP-0002")
    assert product is not None and product["title"] == "Indigo Kurta 2"
    raw = one(
        imports_db.conn,
        "SELECT raw_row FROM import_rows WHERE import_id = %s AND sku = 'MAP-0002'",
        [import_id],
    )
    assert "item sku" in raw and "seller_sku" not in raw


async def test_an_unconfirmed_foreign_or_missing_map_is_refused_alike(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    draft = await api.post(MAPS, headers=headers(imports_db), json={"headers": SELLER})
    unconfirmed = draft.json()["id"]
    theirs = await api.post(
        MAPS, headers=headers(imports_db, "operator@bharat.test"), json={"headers": SELLER}
    )
    answers = []
    for map_id in (unconfirmed, theirs.json()["id"], str(uuid.uuid4())):
        response = await _upload(api, imports_db, _sheet(_seller_row(1)), map_id)
        assert response.status_code == 422, response.text
        assert _code(response) == ("IMPORT_FAILED", ["request"])
        answers.append(response.json()["error"]["error_report"]["errors"][0]["message"])
    assert len(set(answers)) == 1
    assert one(imports_db.conn, "SELECT count(*) FROM imports") == 0


async def test_a_header_that_differs_from_the_map_is_refused(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    map_id = await _confirmed(api, imports_db)
    other = csv_bytes([{"Item SKU": "X-1", "Product Title": "T", "Category": MAPPED[0]}])
    response = await _upload(api, imports_db, other, map_id)
    assert response.status_code == 422 and _code(response) == ("IMPORT_FAILED", ["file.header"])


async def test_another_map_under_the_same_key_is_409(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    first, second = await _confirmed(api, imports_db), await _confirmed(api, imports_db)
    key = str(uuid.uuid4())
    data = _sheet(_seller_row(1))
    assert (await _upload(api, imports_db, data, first, key=key)).status_code == 202
    reused = await _upload(api, imports_db, data, second, key=key)
    assert reused.status_code == 409 and reused.json()["error"]["type"] == "IDEMPOTENCY_KEY_REUSED"


async def test_without_a_map_the_seller_header_is_unknown_as_before(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    response = await _upload(api, imports_db, _sheet(_seller_row(1)), None)
    assert response.status_code == 422
    codes = [e["error_code"] for e in response.json()["error"]["error_report"]["errors"]]
    assert "HEADER_UNKNOWN_COLUMN" in codes


async def test_a_bad_map_id_is_a_validation_error(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    response = await api.post(
        BULK_URL,
        headers={
            **as_member(imports_db, "operator@acme.test"),
            "Idempotency-Key": str(uuid.uuid4()),
        },
        files={"file": ("p.csv", _sheet(_seller_row(1)), "text/csv")},
        data={"column_map_id": "not-a-uuid"},
    )
    assert response.status_code == 422
