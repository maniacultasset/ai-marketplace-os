"""Client feedback 11 Nov (item 5): `POST /catalog/category-suggestions`, its read, and the draft
classification job through the real wrapper on the stub pipeline, as app_rw."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import mkos_pipeline
import pytest
from mkos_pipeline import classify as stub

from . import factories as f
from .assist_support import seed_templates, set_switch
from .conftest import LOCAL_HOSTS
from .images_support import headers, images_db, run_image_job
from .jobs_support import JobWorld, as_member, bad_token, count, job_row

__all__ = ["images_db"]
pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
URL = "/api/v1/catalog/category-suggestions"
CATEGORIES = ("shirts", "t_shirts")
DRAFT = {
    "title": "Cotton crew tee",
    "description": "Soft cotton, round neck.",
    "brand": "Acme",
    "attributes": {"color": "Navy", "fabric_type": "Cotton"},
}


@pytest.fixture(autouse=True)
def _on(images_db: JobWorld, monkeypatch: pytest.MonkeyPatch) -> None:
    seed_templates(images_db.conn, *CATEGORIES)
    set_switch(monkeypatch, "category", on=True)
    stub.LAST_REQUEST = None


async def _ask(
    api: httpx.AsyncClient,
    jw: JobWorld,
    body: dict[str, Any] | None = None,
    *,
    email: str = "operator@acme.test",
    key: str | None = None,
) -> httpx.Response:
    return await api.post(
        URL, json=DRAFT if body is None else body, headers=headers(jw, email, key)
    )


async def _read(
    api: httpx.AsyncClient, jw: JobWorld, suggestion_id: Any, email: str = "reviewer@acme.test"
) -> httpx.Response:
    return await api.get(f"{URL}/{suggestion_id}", headers=as_member(jw, email))


def _code(response: httpx.Response) -> str:
    return str(response.json()["error"]["type"])


def _jobs(jw: JobWorld) -> int:
    return count(
        jw.conn,
        "SELECT count(*) FROM job_runs WHERE entity_type = 'category_suggestion' "
        "AND payload_json ? 'category_draft'",
    )


def _runs(jw: JobWorld, job: Any) -> list[dict[str, Any]]:
    cur = jw.conn.execute("SELECT * FROM agent_runs WHERE job_id = %s", [job])
    names = [d.name for d in cur.description or ()]
    return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]


# ---- the ask ----


@pytest.mark.endpoint("catalog_category_suggest", case="success")
async def test_ask_queues_one_job_and_a_replay_answers_it(
    images_db: JobWorld, api: httpx.AsyncClient
) -> None:
    key = str(uuid.uuid4())
    first = await _ask(api, images_db, key=key)
    assert first.status_code == 202, first.text
    body = first.json()
    assert body["status"] == "queued" and body["candidates"] == []
    assert uuid.UUID(body["suggestion_id"])
    again = await _ask(api, images_db, key=key)
    assert again.status_code == 202 and again.json() == body
    assert _jobs(images_db) == 1
    payload = job_row(images_db.conn, uuid.UUID(body["suggestion_id"]))["payload_json"]
    text = payload["category_draft"]["text"]
    assert text["title"] == "Cotton crew tee" and text["seller_category"] is None
    assert text["attributes"] == {"color": "Navy", "fabric_type": "Cotton"}


async def test_switch_off_is_unavailable_and_spends_nothing(
    images_db: JobWorld, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_switch(monkeypatch, "category", on=False)
    response = await _ask(api, images_db)
    assert response.status_code == 200, response.text
    assert response.json() == {"suggestion_id": None, "status": "unavailable", "candidates": []}
    assert _jobs(images_db) == 0
    assert count(images_db.conn, "SELECT count(*) FROM idempotency_keys") == 0


async def test_ai_paused_503_writes_nothing(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    images_db.conn.execute(
        "UPDATE ai_credit_state SET paused_reason = 'credit_exhausted', paused_at = now() "
        "WHERE id = 1"
    )
    try:
        response = await _ask(api, images_db)
    finally:
        images_db.conn.execute(
            "UPDATE ai_credit_state SET paused_reason = NULL, paused_at = NULL WHERE id = 1"
        )
    assert response.status_code == 503 and _code(response) == "AI_CREDIT_EXHAUSTED"
    assert _jobs(images_db) == 0


async def test_daily_cap_429_writes_nothing(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    tenant = images_db.tenant("acme")
    images_db.conn.execute("UPDATE tenants SET llm_daily_budget_usd = 1 WHERE id = %s", [tenant])
    f.agent_run(images_db.conn, tenant, cost_usd=Decimal("0.999"), started_at=datetime.now(UTC))
    response = await _ask(api, images_db)
    assert response.status_code == 429 and _code(response) == "BUDGET_EXCEEDED"
    assert response.headers["Retry-After"]
    assert _jobs(images_db) == 0


@pytest.mark.endpoint("catalog_category_suggest", case="validation")
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"title": "   "},
        {"title": "Tee", "attributes": {"Bad Key": "x"}},
        {"title": "Tee", "category": "tshirts"},
    ],
)
async def test_bad_body_422(
    images_db: JobWorld, api: httpx.AsyncClient, body: dict[str, Any]
) -> None:
    response = await _ask(api, images_db, body)
    assert response.status_code == 422, response.text


async def test_missing_key_422(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    hdrs = headers(images_db)
    hdrs.pop("Idempotency-Key")
    assert (await api.post(URL, json=DRAFT, headers=hdrs)).status_code == 422


@pytest.mark.endpoint("catalog_category_suggest", case="conflict")
async def test_key_reused_with_another_draft_409(
    images_db: JobWorld, api: httpx.AsyncClient
) -> None:
    key = str(uuid.uuid4())
    assert (await _ask(api, images_db, key=key)).status_code == 202
    reused = await _ask(api, images_db, {**DRAFT, "title": "Silk kurta"}, key=key)
    assert reused.status_code == 409 and _code(reused) == "IDEMPOTENCY_KEY_REUSED"


@pytest.mark.endpoint("catalog_category_suggest", case="wrong_role")
async def test_reviewer_403_owner_202(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    assert (await _ask(api, images_db, email="reviewer@acme.test")).status_code == 403
    assert (await _ask(api, images_db, email="owner@acme.test")).status_code == 202


@pytest.mark.endpoint("catalog_category_suggest", case="wrong_tenant")
async def test_each_tenant_gets_its_own_job(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    key = str(uuid.uuid4())
    acme = await _ask(api, images_db, key=key)
    bharat = await _ask(api, images_db, email="operator@bharat.test", key=key)
    assert acme.status_code == bharat.status_code == 202
    assert acme.json()["suggestion_id"] != bharat.json()["suggestion_id"]
    # each tenant reads only its own
    theirs = await _read(api, images_db, bharat.json()["suggestion_id"])
    assert theirs.status_code == 404


@pytest.mark.endpoint("catalog_category_suggest", case="missing_auth")
async def test_ask_missing_auth(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    response = await api.post(URL, json=DRAFT, headers={"Idempotency-Key": str(uuid.uuid4())})
    assert response.status_code == 401


@pytest.mark.endpoint("catalog_category_suggest", case="invalid_token", variant="expired")
@pytest.mark.endpoint("catalog_category_suggest", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("catalog_category_suggest", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_ask_invalid_token(images_db: JobWorld, api: httpx.AsyncClient, variant: str) -> None:
    hdrs = {**bad_token(images_db, variant), "Idempotency-Key": str(uuid.uuid4())}
    assert (await api.post(URL, json=DRAFT, headers=hdrs)).status_code == 401


# ---- the job and the read ----


async def _asked(api: httpx.AsyncClient, jw: JobWorld, title: str = "Cotton crew tee") -> uuid.UUID:
    response = await _ask(api, jw, {**DRAFT, "title": title})
    assert response.status_code == 202, response.text
    return uuid.UUID(response.json()["suggestion_id"])


@pytest.mark.endpoint("catalog_category_suggestion_get", case="success")
async def test_queued_then_suggested_with_its_run(
    images_db: JobWorld, api: httpx.AsyncClient
) -> None:
    job = await _asked(api, images_db)
    queued = await _read(api, images_db, job)
    assert queued.status_code == 200, queued.text
    assert queued.json() == {"suggestion_id": str(job), "status": "queued", "candidates": []}
    await run_image_job(job)
    assert job_row(images_db.conn, job)["status"] == "succeeded"
    read = await _read(api, images_db, job)
    assert read.status_code == 200, read.text
    assert read.json() == {
        "suggestion_id": str(job),
        "status": "suggested",
        "candidates": [
            {
                "category": "shirts",
                "label": "Shirts",
                "confidence": 0.92,
                "rationale": "The title fits.",
                "unsure": False,
            }
        ],
    }
    request = stub.LAST_REQUEST
    assert request is not None and request.images == []
    assert request.product_id == job and request.text.seller_category is None
    assert [c.category for c in request.categories] == list(CATEGORIES)
    (run,) = _runs(images_db, job)
    assert (run["agent_name"], run["trigger"]) == ("attribute", "generate")
    assert run["product_id"] is None and run["listing_id"] is None
    assert count(images_db.conn, "SELECT count(*) FROM human_tasks") == 0
    assert Decimal(job_row(images_db.conn, job)["cost_reserved_usd"]) == 0


async def test_low_confidence_is_marked_unsure(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    job = await _asked(api, images_db, "Tee [lowconf]")
    await run_image_job(job)
    (candidate,) = (await _read(api, images_db, job)).json()["candidates"]
    assert candidate["confidence"] == 0.5 and candidate["unsure"] is True


@pytest.mark.parametrize("marker", ["[none]", "[outside]"])
async def test_nothing_fits_is_suggested_with_no_candidate(
    images_db: JobWorld, api: httpx.AsyncClient, marker: str
) -> None:
    job = await _asked(api, images_db, f"Tee {marker}")
    await run_image_job(job)
    body = (await _read(api, images_db, job)).json()
    assert body["status"] == "suggested" and body["candidates"] == []


async def test_untrusted_record_fails_the_suggestion(
    images_db: JobWorld, api: httpx.AsyncClient
) -> None:
    job = await _asked(api, images_db, "Tee [badrecord]")
    await run_image_job(job)
    assert job_row(images_db.conn, job)["status"] != "succeeded"
    assert (await _read(api, images_db, job)).json()["status"] == "failed"
    assert _runs(images_db, job) == []


async def test_pipeline_without_the_entry_is_unavailable(
    images_db: JobWorld, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = await _asked(api, images_db)
    monkeypatch.delattr(mkos_pipeline, "classify_product")
    await run_image_job(job)
    assert (await _read(api, images_db, job)).json()["status"] == "unavailable"
    assert _runs(images_db, job) == []


async def test_switch_off_after_the_ask_is_unavailable_with_no_call(
    images_db: JobWorld, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = await _asked(api, images_db)
    set_switch(monkeypatch, "category", on=False)
    await run_image_job(job)
    assert (await _read(api, images_db, job)).json()["status"] == "unavailable"
    assert stub.LAST_REQUEST is None


async def test_parallel_asks_under_one_key_make_one_job(
    images_db: JobWorld, api: httpx.AsyncClient
) -> None:
    key = str(uuid.uuid4())
    first, second = await asyncio.gather(
        _ask(api, images_db, key=key), _ask(api, images_db, key=key)
    )
    assert 202 in {first.status_code, second.status_code}
    assert _jobs(images_db) == 1


# ---- the same draft asked again ----


async def test_same_draft_again_answers_the_live_job(
    images_db: JobWorld, api: httpx.AsyncClient
) -> None:
    first = await _ask(api, images_db)
    second = await _ask(api, images_db)
    assert first.status_code == second.status_code == 202
    assert second.json() == first.json()
    assert _jobs(images_db) == 1
    changed = await _ask(api, images_db, {**DRAFT, "title": "Linen crew tee"})
    assert changed.status_code == 202
    assert changed.json()["suggestion_id"] != first.json()["suggestion_id"]
    assert _jobs(images_db) == 2


async def test_same_draft_after_it_answered_is_200_with_no_new_call(
    images_db: JobWorld, api: httpx.AsyncClient
) -> None:
    job = await _asked(api, images_db)
    await run_image_job(job)
    stub.LAST_REQUEST = None
    again = await _ask(api, images_db)
    assert again.status_code == 200, again.text
    assert again.json() == (await _read(api, images_db, job)).json()
    assert again.json()["status"] == "suggested" and again.json()["candidates"]
    assert _jobs(images_db) == 1 and stub.LAST_REQUEST is None
    assert len(_runs(images_db, job)) == 1


async def test_same_draft_after_a_day_or_a_failure_asks_again(
    images_db: JobWorld, api: httpx.AsyncClient
) -> None:
    job = await _asked(api, images_db)
    await run_image_job(job)
    images_db.conn.execute(
        "UPDATE job_runs SET queued_at = queued_at - interval '25 hours' WHERE id = %s", [job]
    )
    later = await _ask(api, images_db)
    assert later.status_code == 202 and later.json()["suggestion_id"] != str(job)
    failed = await _asked(api, images_db, "Tee [badrecord]")
    await run_image_job(failed)
    retried = await _ask(api, images_db, {**DRAFT, "title": "Tee [badrecord]"})
    assert retried.status_code == 202 and retried.json()["suggestion_id"] != str(failed)
    assert _jobs(images_db) == 4


async def test_widest_draft_fits_the_job_payload(
    images_db: JobWorld, api: httpx.AsyncClient
) -> None:
    emoji = "\U0001f600"
    body = {
        "title": emoji * 500,
        "description": emoji * 5000,
        "brand": emoji * 200,
        "attributes": {f"k{i:02d}": emoji * 200 for i in range(60)},
    }
    response = await _ask(api, images_db, body)
    assert response.status_code == 202, response.text
    payload = job_row(images_db.conn, uuid.UUID(response.json()["suggestion_id"]))["payload_json"]
    text = payload["category_draft"]["text"]
    assert text["title"] == emoji * 500 and 0 < len(text["attributes"]) < 40
    assert len(payload["category_draft"]["digest"]) == 64


@pytest.mark.endpoint("catalog_category_suggestion_get", case="validation")
async def test_read_bad_id_422(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    response = await api.get(f"{URL}/not-a-uuid", headers=as_member(images_db, "owner@acme.test"))
    assert response.status_code == 422


@pytest.mark.endpoint("catalog_category_suggestion_get", case="not_found")
async def test_read_unknown_or_product_job_404(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    from .ops_support import job_run

    assert (await _read(api, images_db, uuid.uuid4())).status_code == 404
    # a job that is not a draft suggestion reads as missing too
    other = job_run(images_db.conn, images_db.tenant("acme"))
    assert (await _read(api, images_db, other)).status_code == 404


@pytest.mark.endpoint("catalog_category_suggestion_get", case="wrong_tenant")
async def test_read_other_tenants_404_like_a_missing_one(
    images_db: JobWorld, api: httpx.AsyncClient
) -> None:
    theirs = await _ask(api, images_db, email="operator@bharat.test")
    foreign = await _read(api, images_db, theirs.json()["suggestion_id"])
    missing = await _read(api, images_db, uuid.uuid4())
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["error"]["message"] == missing.json()["error"]["message"]


@pytest.mark.endpoint("catalog_category_suggestion_get", case="missing_auth")
async def test_read_missing_auth(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    assert (await api.get(f"{URL}/{uuid.uuid4()}")).status_code == 401


@pytest.mark.endpoint("catalog_category_suggestion_get", case="invalid_token", variant="expired")
@pytest.mark.endpoint(
    "catalog_category_suggestion_get", case="invalid_token", variant="bad_signature"
)
@pytest.mark.endpoint("catalog_category_suggestion_get", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_read_invalid_token(
    images_db: JobWorld, api: httpx.AsyncClient, variant: str
) -> None:
    response = await api.get(f"{URL}/{uuid.uuid4()}", headers=bad_token(images_db, variant))
    assert response.status_code == 401
