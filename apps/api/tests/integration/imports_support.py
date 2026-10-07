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
