"""SI9 T13 (AC16; CP-D19): `GET /api/v1/products/{id}` shows the newest photo read of the product
(`read` with counts, `unchanged`, `failed`, `not_read`); list rows show null."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world
from .photo_support import OPERATOR, PhotoWorld, generate, photo_world, regenerable
from .review_support import generated

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world", "photo_world"]


async def _read(api: httpx.AsyncClient, world: GenWorld, product_id: Any) -> Any:
    response = await api.get(f"/api/v1/products/{product_id}", headers=world.headers(OPERATOR))
    assert response.status_code == 200, response.text
    return response.json()["photo_read"]


async def test_read_then_unchanged(api: httpx.AsyncClient, photo_world: PhotoWorld) -> None:
    pw = photo_world
    accepted = await generate(pw, "photo_ok")
    first = await _read(api, pw.world, pw.product_id)
    assert first["status"] == "read" and first["written"] >= 1 and first["asked"] == 0
    assert first["at"]
    regenerable(pw, accepted.listing_id)
    await generate(pw, "photo_ok")
    again = await _read(api, pw.world, pw.product_id)
    assert again == {**again, "status": "unchanged", "written": 0, "asked": 0}


async def test_a_failed_photo_call_reads_failed(
    api: httpx.AsyncClient, photo_world: PhotoWorld
) -> None:
    pw = photo_world
    accepted = await generate(pw, "photo_timeout")
    assert pw.world.listing(accepted.listing_id)["current_version_id"] is not None
    got = await _read(api, pw.world, pw.product_id)
    assert (got["status"], got["written"], got["asked"]) == ("failed", 0, 0)


async def test_no_photo_reads_not_read(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    product = gen_world.scalar("SELECT product_id FROM listings WHERE id = %s", [made.listing_id])
    got = await _read(api, gen_world, product)
    assert got["status"] == "not_read"


async def test_no_generation_and_lists_are_null(
    api: httpx.AsyncClient, photo_world: PhotoWorld
) -> None:
    pw = photo_world
    assert await _read(api, pw.world, pw.product_id) is None
    await generate(pw, "photo_ok")
    page = await api.get("/api/v1/products", headers=pw.world.headers(OPERATOR))
    assert page.status_code == 200, page.text
    assert all(row["photo_read"] is None for row in page.json()["data"])


async def test_a_malformed_record_reads_null(
    api: httpx.AsyncClient, photo_world: PhotoWorld
) -> None:
    pw = photo_world
    accepted = await generate(pw, "photo_ok")
    with pw.conn.transaction():  # a terminal job row is guarded: set-up only, guards off
        pw.conn.execute("SET LOCAL session_replication_role = replica")
        pw.conn.execute(
            "UPDATE job_runs SET result_json = jsonb_set(result_json, '{photo_read}', "
            '\'{"status": "reading", "written": 99}\'::jsonb) WHERE id = %s',
            [accepted.job_id],
        )
    assert await _read(api, pw.world, pw.product_id) is None
