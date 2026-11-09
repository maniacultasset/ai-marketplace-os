"""SI14 T11-T12 (AC3, AC4): `POST /products/{id}/category-suggestion` as app_rw, and the
classification a create without a category queues."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest

from . import factories as f
from .assist_support import seed_templates, set_switch, suggest_jobs, suggestion
from .conftest import LOCAL_HOSTS
from .images_support import headers, images_db, style_and_child
from .jobs_support import JobWorld, bad_token, count

__all__ = ["images_db"]
pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
PREFIX = "/api/v1/products"


@pytest.fixture(autouse=True)
def _on(images_db: JobWorld, monkeypatch: pytest.MonkeyPatch) -> None:
    seed_templates(images_db.conn, "t_shirts", "shirts")
    set_switch(monkeypatch, "category", on=True)


def _url(product_id: Any) -> str:
    return f"{PREFIX}/{product_id}/category-suggestion"


async def _ask(
    api: httpx.AsyncClient,
    jw: JobWorld,
    product_id: Any,
    *,
    email: str = "operator@acme.test",
    key: str | None = None,
) -> httpx.Response:
    return await api.post(_url(product_id), headers=headers(jw, email, key))


def _code(response: httpx.Response) -> str:
    return str(response.json()["error"]["type"])


def _product(jw: JobWorld, **cols: Any) -> uuid.UUID:
    return f.product(jw.conn, jw.tenant("acme"), **cols)


def _runs(jw: JobWorld) -> int:
    return count(jw.conn, "SELECT count(*) FROM agent_runs")


@pytest.mark.endpoint("products_category_suggest", case="success")
async def test_ask_enqueues_one_job_and_a_replay_returns_it(
    images_db: JobWorld, api: httpx.AsyncClient
) -> None:
    p = _product(images_db)
    key = str(uuid.uuid4())
    first = await _ask(api, images_db, p, key=key)
    assert first.status_code == 202, first.text
    body = first.json()
    assert body["status"] == "queued" and body["product_id"] == str(p)
    again = await _ask(api, images_db, p, key=key)
    assert again.status_code == 202 and again.json() == body
    assert suggest_jobs(images_db.conn, p) == 1
    # a second ask under a new key while the job is live answers that job
    live = await _ask(api, images_db, p)
    assert live.status_code == 202 and live.json()["job_id"] == body["job_id"]
    assert suggest_jobs(images_db.conn, p) == 1
    assert suggestion(images_db.conn, p) is None  # nothing stored until the job runs


async def test_switch_off_is_unavailable_and_spends_nothing(
    images_db: JobWorld, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_switch(monkeypatch, "category", on=False)
    p = _product(images_db)
    response = await _ask(api, images_db, p)
    assert response.status_code == 200, response.text
    assert response.json() == {"product_id": str(p), "status": "unavailable", "job_id": None}
    assert suggest_jobs(images_db.conn, p) == 0 and _runs(images_db) == 0
    assert count(images_db.conn, "SELECT count(*) FROM job_runs") == 0


def _store_suggested(
    jw: JobWorld, product_id: uuid.UUID, digest: str, status: str = "suggested"
) -> None:
    from psycopg.types.json import Jsonb

    jw.conn.execute(
        "UPDATE products SET category_suggestion = %s WHERE id = %s",
        [
            Jsonb(
                {
                    "status": status,
                    "candidates": [
                        {
                            "category": "t_shirts",
                            "confidence": 0.9,
                            "rationale": "A tee.",
                            "evidence_refs": [],
                        }
                    ],
                    "input_digest": digest,
                    "calibration_version": "v1",
                    "job_id": None,
                    "at": datetime.now(UTC).isoformat(),
                }
            ),
            product_id,
        ],
    )


async def _digest(jw: JobWorld, product_id: uuid.UUID) -> str:
    from app.core.config import get_settings
    from app.repositories.uow import tenant_uow
    from app.services.category_suggest import suggest_inputs

    async with tenant_uow(jw.tenant("acme")) as uow:
        product = await uow.products.get_by_id(product_id)
        assert product is not None
        return (await suggest_inputs(uow, product, get_settings())).digest


async def test_same_digest_answers_200_and_a_title_edit_asks_again(
    images_db: JobWorld, api: httpx.AsyncClient
) -> None:
    p = _product(images_db)
    _store_suggested(images_db, p, await _digest(images_db, p))
    cached = await _ask(api, images_db, p)
    assert cached.status_code == 200 and cached.json()["status"] == "suggested"
    assert suggest_jobs(images_db.conn, p) == 0
    images_db.conn.execute("UPDATE products SET title = 'A new title' WHERE id = %s", [p])
    asked = await _ask(api, images_db, p)
    assert asked.status_code == 202 and suggest_jobs(images_db.conn, p) == 1


@pytest.mark.parametrize("status", ["confirmed", "overridden"])
async def test_a_settled_suggestion_for_the_same_digest_answers_200(
    images_db: JobWorld, api: httpx.AsyncClient, status: str
) -> None:
    p = _product(images_db)
    _store_suggested(images_db, p, await _digest(images_db, p), status)
    cached = await _ask(api, images_db, p)
    assert cached.status_code == 200, cached.text
    assert cached.json() == {"product_id": str(p), "status": status, "job_id": None}
    assert suggest_jobs(images_db.conn, p) == 0


async def test_ai_paused_503_writes_nothing(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    p = _product(images_db)
    images_db.conn.execute(
        "UPDATE ai_credit_state SET paused_reason = 'credit_exhausted', paused_at = now() "
        "WHERE id = 1"
    )
    try:
        response = await _ask(api, images_db, p)
    finally:
        images_db.conn.execute(
            "UPDATE ai_credit_state SET paused_reason = NULL, paused_at = NULL WHERE id = 1"
        )
    assert response.status_code == 503 and _code(response) == "AI_CREDIT_EXHAUSTED"
    assert suggest_jobs(images_db.conn, p) == 0
    assert count(images_db.conn, "SELECT count(*) FROM idempotency_keys") == 0


async def test_daily_cap_429_writes_nothing(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    tenant = images_db.tenant("acme")
    p = _product(images_db)
    images_db.conn.execute("UPDATE tenants SET llm_daily_budget_usd = 1 WHERE id = %s", [tenant])
    f.agent_run(images_db.conn, tenant, cost_usd=Decimal("0.999"), started_at=datetime.now(UTC))
    response = await _ask(api, images_db, p)
    assert response.status_code == 429 and _code(response) == "BUDGET_EXCEEDED"
    assert suggest_jobs(images_db.conn, p) == 0


async def test_parallel_asks_make_one_job(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    p = _product(images_db)
    first, second = await asyncio.gather(_ask(api, images_db, p), _ask(api, images_db, p))
    assert {first.status_code, second.status_code} == {202}
    assert first.json()["job_id"] == second.json()["job_id"]
    assert suggest_jobs(images_db.conn, p) == 1


@pytest.mark.endpoint("products_category_suggest", case="validation")
async def test_variant_and_missing_key(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    _style, child = style_and_child(images_db.conn, images_db.tenant("acme"))
    response = await _ask(api, images_db, child)
    assert response.status_code == 422 and _code(response) == "FACT_LEVEL_INVALID"
    hdrs = headers(images_db)
    hdrs.pop("Idempotency-Key")
    response = await api.post(_url(_product(images_db)), headers=hdrs)
    assert response.status_code == 422


@pytest.mark.endpoint("products_category_suggest", case="conflict")
async def test_key_reused_on_another_product_409(
    images_db: JobWorld, api: httpx.AsyncClient
) -> None:
    key = str(uuid.uuid4())
    assert (await _ask(api, images_db, _product(images_db), key=key)).status_code == 202
    reused = await _ask(api, images_db, _product(images_db), key=key)
    assert reused.status_code == 409 and _code(reused) == "IDEMPOTENCY_KEY_REUSED"


@pytest.mark.endpoint("products_category_suggest", case="wrong_role")
async def test_reviewer_403_owner_202(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    p = _product(images_db)
    assert (await _ask(api, images_db, p, email="reviewer@acme.test")).status_code == 403
    assert (await _ask(api, images_db, p, email="owner@acme.test")).status_code == 202


@pytest.mark.endpoint("products_category_suggest", case="not_found")
async def test_unknown_product_404(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    response = await _ask(api, images_db, uuid.uuid4())
    assert response.status_code == 404 and _code(response) == "NOT_FOUND"


@pytest.mark.endpoint("products_category_suggest", case="wrong_tenant")
async def test_other_tenants_product_404_like_a_missing_one(
    images_db: JobWorld, api: httpx.AsyncClient
) -> None:
    theirs = f.product(images_db.conn, images_db.tenant("bharat"))
    foreign = await _ask(api, images_db, theirs)
    missing = await _ask(api, images_db, uuid.uuid4())
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["error"]["message"] == missing.json()["error"]["message"]
    assert suggest_jobs(images_db.conn, theirs) == 0


@pytest.mark.endpoint("products_category_suggest", case="missing_auth")
async def test_missing_auth(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    response = await api.post(_url(uuid.uuid4()), headers={"Idempotency-Key": str(uuid.uuid4())})
    assert response.status_code == 401


@pytest.mark.endpoint("products_category_suggest", case="invalid_token", variant="expired")
@pytest.mark.endpoint("products_category_suggest", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("products_category_suggest", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_invalid_token(images_db: JobWorld, api: httpx.AsyncClient, variant: str) -> None:
    hdrs = {**bad_token(images_db, variant), "Idempotency-Key": str(uuid.uuid4())}
    response = await api.post(_url(uuid.uuid4()), headers=hdrs)
    assert response.status_code == 401


async def test_product_read_shows_switch_and_suggestion(
    images_db: JobWorld, api: httpx.AsyncClient
) -> None:
    from .jobs_support import as_member

    p = _product(images_db)
    _store_suggested(images_db, p, "0" * 64)
    read = await api.get(f"{PREFIX}/{p}", headers=as_member(images_db, "reviewer@acme.test"))
    assert read.status_code == 200, read.text
    body = read.json()
    assert body["category_suggest_enabled"] is True
    assert body["suggestions"]["category"]["status"] == "suggested"
    assert body["suggestions"]["category"]["candidates"] == [
        {"category": "t_shirts", "confidence": 0.9, "rationale": "A tee."}
    ]
    assert "input_digest" not in str(body) and "evidence_refs" not in str(body)


# ---- T12: a create without a category queues one classification ----


async def _create(api: httpx.AsyncClient, jw: JobWorld, sku: str, **body: Any) -> httpx.Response:
    from .jobs_support import as_member

    return await api.post(
        PREFIX,
        headers=as_member(jw, "operator@acme.test"),
        json={"sku": sku, "title": "Cotton crew tee", **body},
    )


def _created_jobs(jw: JobWorld) -> int:
    return count(jw.conn, "SELECT count(*) FROM job_runs WHERE entity_type = 'category_suggestion'")


async def test_create_without_category_queues_one_job(
    images_db: JobWorld, api: httpx.AsyncClient
) -> None:
    created = await _create(api, images_db, "NC-1")
    assert created.status_code == 201, created.text
    p = uuid.UUID(created.json()["id"])
    assert suggest_jobs(images_db.conn, p) == 1
    # the same SKU again is 409 SKU_CONFLICT and queues nothing more (products_create takes no key)
    again = await _create(api, images_db, "NC-1")
    assert again.status_code == 409 and _code(again) == "SKU_CONFLICT"
    assert _created_jobs(images_db) == 1


async def test_create_with_category_or_switch_off_queues_nothing(
    images_db: JobWorld, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (await _create(api, images_db, "NC-2", category="t_shirts")).status_code == 201
    set_switch(monkeypatch, "category", on=False)
    assert (await _create(api, images_db, "NC-3")).status_code == 201
    assert _created_jobs(images_db) == 0


async def test_paused_ai_still_creates_with_no_job(
    images_db: JobWorld, api: httpx.AsyncClient
) -> None:
    images_db.conn.execute(
        "UPDATE ai_credit_state SET paused_reason = 'credit_exhausted', paused_at = now() "
        "WHERE id = 1"
    )
    try:
        created = await _create(api, images_db, "NC-4")
    finally:
        images_db.conn.execute(
            "UPDATE ai_credit_state SET paused_reason = NULL, paused_at = NULL WHERE id = 1"
        )
    assert created.status_code == 201, created.text
    assert created.json()["category"] is None
    assert _created_jobs(images_db) == 0
