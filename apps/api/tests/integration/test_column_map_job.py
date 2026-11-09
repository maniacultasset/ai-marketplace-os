"""SI14 T17 (AC8): the column-map job through the real wrapper and the stub `map_columns`: only the
leftover headers are sent, never a sample cell, and the answers are screened."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import mkos_pipeline
import pytest
from mkos_pipeline import columns as stub
from mkos_schemas.columns import ColumnMapping, ColumnMapResult

from .assist_support import set_switch
from .conftest import LOCAL_HOSTS
from .images_support import run_image_job
from .jobs_support import JobWorld, as_member, job_row, jobs_db

__all__ = ["jobs_db"]
pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
MAPS = "/api/v1/imports/column-maps"
REQUIRED = ["Item SKU", "product_name", "category"]
# leftovers the deterministic rungs cannot map, each holding a dictionary key for the stub
BRAND = "internal ref code for brand_name ops team"
LOWCONF = "lowconf internal style_number reference column"
OUTSIDE = "outside system vendor field reference"


@pytest.fixture(autouse=True)
def _on(monkeypatch: pytest.MonkeyPatch) -> None:
    set_switch(monkeypatch, "columns", on=True)
    stub.LAST_REQUEST = None


async def _made(
    api: httpx.AsyncClient,
    jw: JobWorld,
    headers: list[str],
    samples: dict[str, list[str]] | None = None,
) -> tuple[str, uuid.UUID]:
    response = await api.post(
        MAPS,
        headers={**as_member(jw, "operator@acme.test"), "Idempotency-Key": str(uuid.uuid4())},
        json={"headers": headers, "samples": samples or {}},
    )
    assert response.status_code == 202, response.text
    map_id = response.json()["id"]
    job = jw.conn.execute(
        "SELECT id FROM job_runs WHERE entity_type = 'column_map' AND entity_id = %s", [map_id]
    ).fetchone()
    assert job is not None
    return map_id, job[0]


async def _get(api: httpx.AsyncClient, jw: JobWorld, map_id: str) -> dict[str, Any]:
    response = await api.get(f"{MAPS}/{map_id}", headers=as_member(jw, "operator@acme.test"))
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _runs(jw: JobWorld, job: uuid.UUID) -> list[dict[str, Any]]:
    cur = jw.conn.execute("SELECT * FROM agent_runs WHERE job_id = %s", [job])
    names = [d.name for d in cur.description or ()]
    return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]


async def test_only_leftovers_are_sent_and_never_a_sample(
    jobs_db: JobWorld, api: httpx.AsyncClient
) -> None:
    samples = {BRAND: ["12 MG Road, Pune 411001", "Asha Verma"]}
    map_id, job = await _made(api, jobs_db, [*REQUIRED, "Colour", BRAND], samples)
    await run_image_job(job)
    assert job_row(jobs_db.conn, job)["status"] == "succeeded"
    request = stub.LAST_REQUEST
    assert request is not None
    assert [c.header for c in request.headers] == [BRAND]
    assert all(c.samples == [] for c in request.headers)
    assert "MG Road" not in request.model_dump_json() and "Asha" not in request.model_dump_json()
    assert set(request.taken_keys) == {"seller_sku", "product_name", "category", "color_name"}
    body = await _get(api, jobs_db, map_id)
    assert body["status"] == "ready"
    last = body["mappings"][-1]
    assert (last["key"], last["method"], last["needs_review"]) == ("brand_name", "ai", False)
    (run,) = _runs(jobs_db, job)
    assert (run["agent_name"], run["trigger"]) == ("attribute", "generate")
    assert run["product_id"] is None and run["listing_id"] is None


async def test_low_confidence_needs_review_and_outside_keys_are_dropped(
    jobs_db: JobWorld, api: httpx.AsyncClient
) -> None:
    map_id, job = await _made(api, jobs_db, [*REQUIRED, LOWCONF, OUTSIDE])
    await run_image_job(job)
    low, outside = (await _get(api, jobs_db, map_id))["mappings"][3:]
    assert (low["key"], low["method"], low["confidence"], low["needs_review"]) == (
        "style_number",
        "ai",
        0.79,
        True,
    )
    assert (outside["key"], outside["method"], outside["needs_review"]) == (None, "none", True)


async def test_a_key_an_exact_column_holds_is_dropped(
    jobs_db: JobWorld, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.workers import column_map_seam

    async def steal(request: Any, credentials: Any) -> Any:
        real = await stub.map_columns(request, credentials)
        stolen = {
            c.header: ColumnMapping(key="seller_sku", confidence=0.99) for c in request.headers
        }
        return ColumnMapResult(
            status="ok", mappings=stolen, confidence_version="x", agent_runs=real.agent_runs
        )

    monkeypatch.setattr(column_map_seam, "call_map_columns", steal)
    map_id, job = await _made(api, jobs_db, [*REQUIRED, BRAND])
    await run_image_job(job)
    assert (await _get(api, jobs_db, map_id))["mappings"][3]["key"] is None


async def test_leftovers_over_the_seam_cap(jobs_db: JobWorld, api: httpx.AsyncClient) -> None:
    extra = [f"unmapped vendor field {n:03d} brand_name" for n in range(70)]
    map_id, job = await _made(api, jobs_db, [*REQUIRED, *extra])
    await run_image_job(job)
    assert job_row(jobs_db.conn, job)["status"] == "succeeded"
    assert stub.LAST_REQUEST is not None and len(stub.LAST_REQUEST.headers) == 60
    mappings = (await _get(api, jobs_db, map_id))["mappings"]
    tail = mappings[3 + 60 :]
    assert len(tail) == 10 and all(m["key"] is None and m["needs_review"] for m in tail)
    # one answer per identity: all 60 asked for brand_name, only one keeps it
    assert sum(1 for m in mappings if m["key"] == "brand_name") == 1


async def test_pipeline_without_the_entry_is_ready_with_no_spend(
    jobs_db: JobWorld, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delattr(mkos_pipeline, "map_columns")
    map_id, job = await _made(api, jobs_db, [*REQUIRED, BRAND])
    await run_image_job(job)
    body = await _get(api, jobs_db, map_id)
    assert body["status"] == "ready" and body["mappings"][3]["key"] is None
    assert _runs(jobs_db, job) == []


async def test_exhausted_retries_fail_the_map_and_confirm_still_works(
    jobs_db: JobWorld, api: httpx.AsyncClient
) -> None:
    map_id, job = await _made(api, jobs_db, [*REQUIRED, "boom column"])
    runs = await run_image_job(job, max_runs=5)
    assert runs == 3 and job_row(jobs_db.conn, job)["status"] == "dead_letter"
    assert (await _get(api, jobs_db, map_id))["status"] == "failed"
    confirmed = await api.post(
        f"{MAPS}/{map_id}/confirm",
        headers={**as_member(jobs_db, "operator@acme.test"), "Idempotency-Key": str(uuid.uuid4())},
        json={"mappings": [{"position": 4, "key": None}]},
    )
    assert confirmed.status_code == 200 and confirmed.json()["status"] == "confirmed"


async def test_a_second_run_on_a_ready_map_is_a_no_op(
    jobs_db: JobWorld, api: httpx.AsyncClient
) -> None:
    from app.workers.column_map import run_column_map
    from app.workers.registry import JobContext

    map_id, job = await _made(api, jobs_db, [*REQUIRED, BRAND])
    await run_image_job(job)
    before = await _get(api, jobs_db, map_id)
    from mkos_schemas.enums import JobKind

    ctx = JobContext(
        job_run_id=job,
        tenant_id=jobs_db.tenant("acme"),
        kind=JobKind.IMAGE_COMPLIANCE,
        attempt=2,
        payload={"column_map": {"map_id": map_id}},
        request_id="retry",
    )
    outcome = await run_column_map(ctx)
    assert outcome.result is not None and outcome.result["status"] == "skipped"
    assert await _get(api, jobs_db, map_id) == before
