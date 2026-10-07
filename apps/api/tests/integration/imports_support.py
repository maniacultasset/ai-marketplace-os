"""The S13b CSV-import test world: the job world (`acme`, `bharat`, one member per role, an
empty queue) plus the two seed categories' templates (`ethnic_wear_kurta_sets`,
`home_decor_lamps`), the synced dictionary, the compose MinIO, and CSV builders."""

from __future__ import annotations

import csv
import io
import uuid
from collections.abc import AsyncIterator, Iterable, Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
from psycopg.types.json import Jsonb

from .auth_support import PREFIX
from .jobs_support import Conn, JobWorld, as_member, reset_queue

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/imports"
BULK_URL = f"{PREFIX}/products/bulk"
MAPPED = ("ethnic_wear_kurta_sets", "home_decor_lamps")
UNMAPPED = "ethnic_wear_dupattas"
IMAGE = "https://cdn.example.com/p/{n}.jpg"
EMPTY_TEMPLATE: dict[str, Any] = {
    "required_attributes": [],
    "optional_attributes": [],
    "specs": {},
}


def seed_templates(conn: Conn, categories: Iterable[str] = MAPPED) -> None:
    for category in categories:
        for marketplace in ("amazon", "flipkart"):
            conn.execute(
                "INSERT INTO category_templates (marketplace, category, version, is_current, "
                "product_type, source, attributes) VALUES (%s, %s, 1, true, 'X', 'manual', %s) "
                "ON CONFLICT DO NOTHING",
                [marketplace, category, Jsonb(EMPTY_TEMPLATE)],
            )


@pytest.fixture
async def imports_db(auth_db: Conn) -> AsyncIterator[JobWorld]:
    from app.adapters.storage import get_storage
    from app.db.queue import get_queue_app

    from .auth_support import build_world

    reset_queue(auth_db)
    seed_templates(auth_db)
    get_queue_app.cache_clear()
    get_storage.cache_clear()
    yield JobWorld(auth_db, build_world(auth_db))
    get_queue_app.cache_clear()
    get_storage.cache_clear()


def row(n: int, **cells: str) -> dict[str, str]:
    base = {
        "seller_sku": f"SKU-{n:05d}",
        "product_name": f"Cotton Kurta {n}",
        "category": MAPPED[0],
        "mrp": "1299",
        "selling_price": "999",
        "main_image": IMAGE.format(n=n),
    }
    return {**base, **cells}


def csv_bytes(rows: Iterable[Mapping[str, str]], header: list[str] | None = None) -> bytes:
    rows = list(rows)
    columns = header or list(dict.fromkeys(k for r in rows for k in r))
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=columns, lineterminator="\n", extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow({k: r.get(k, "") for k in columns})
    return out.getvalue().encode("utf-8")


def headers(
    jw: JobWorld, email: str = "operator@acme.test", key: str | None = None
) -> dict[str, str]:
    return {**as_member(jw, email), "Idempotency-Key": key or str(uuid.uuid4())}


async def upload(
    api: httpx.AsyncClient,
    hdrs: Mapping[str, str],
    data: bytes,
    filename: str = "products.csv",
) -> httpx.Response:
    return await api.post(
        BULK_URL, headers=dict(hdrs), files={"file": (filename, data, "text/csv")}
    )


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def one(conn: Conn, query: str, params: list[Any] | None = None) -> Any:
    found = conn.execute(query, params or []).fetchone()
    assert found is not None
    return found[0]


async def import_fixture(
    jw: JobWorld, api: httpx.AsyncClient, data: bytes, email: str = "operator@acme.test"
) -> tuple[uuid.UUID, uuid.UUID]:
    """Upload `data` through the API; returns (import_id, job_id)."""
    response = await upload(api, headers(jw, email), data)
    assert response.status_code == 202, response.text
    body = response.json()
    return uuid.UUID(body["import_id"]), uuid.UUID(body["job_id"])


async def run_job(job_id: uuid.UUID) -> None:
    """The job's body through the real wrapper (claim, body, terminal write and hook)."""
    from app.workers import wrapper
    from app.workers.tasks import register_bodies

    register_bodies()
    await wrapper.run_job(str(job_id))


def import_row(conn: Conn, import_id: uuid.UUID) -> dict[str, Any]:
    cur = conn.execute("SELECT * FROM imports WHERE id = %s", [import_id])
    names = [d.name for d in cur.description or ()]
    found = cur.fetchone()
    assert found is not None
    return dict(zip(names, found, strict=True))


def row_results(conn: Conn, import_id: uuid.UUID) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT row_number, status, sku, product_id, error_report_json FROM import_rows "
        "WHERE import_id = %s ORDER BY row_number",
        [import_id],
    ).fetchall()
