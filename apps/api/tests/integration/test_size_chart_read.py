"""SI12 T18-T19 (AC4-AC7): `POST /products/{id}/size-chart-read` as app_rw, on a fake object store
and the stub pipeline, and `size_chart_suggestion` on the product read."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
from psycopg.types.json import Jsonb

from . import factories as f
from .conftest import LOCAL_HOSTS
from .images_support import FakeStorage, headers, images_db, run_image_job, style_and_child
from .jobs_support import JobWorld, as_member, bad_token, count, job_row
from .size_chart_support import chart_image, read_state, use_read_storage

__all__ = ["images_db"]
pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
PREFIX = "/api/v1/products"


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStorage:
    fake = FakeStorage()
    use_read_storage(monkeypatch, fake)
    return fake


def _url(product_id: Any) -> str:
    return f"{PREFIX}/{product_id}/size-chart-read"


async def _read(
    api: httpx.AsyncClient,
    jw: JobWorld,
    product_id: Any,
    *,
    email: str = "operator@acme.test",
    key: str | None = None,
) -> httpx.Response:
    return await api.post(_url(product_id), headers=headers(jw, email, key))


async def _detail(api: httpx.AsyncClient, jw: JobWorld, product_id: Any) -> dict[str, Any]:
    response = await api.get(f"{PREFIX}/{product_id}", headers=as_member(jw, "reviewer@acme.test"))
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _jobs(jw: JobWorld, image_id: Any) -> int:
    return count(
        jw.conn,
        "SELECT count(*) FROM job_runs WHERE entity_type = 'size_chart_read' AND entity_id = %s",
        [uuid.UUID(str(image_id))],
    )


def _runs(jw: JobWorld, tenant: uuid.UUID) -> int:
    return count(jw.conn, "SELECT count(*) FROM agent_runs WHERE tenant_id = %s", [tenant])


def _world(jw: JobWorld, store: FakeStorage) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    tenant = jw.tenant("acme")
    p = f.product(jw.conn, tenant, category="tshirts")
    image, _ = chart_image(jw.conn, store, tenant, p)
    return tenant, p, image


def _code(response: httpx.Response) -> str:
    code: str = response.json()["error"]["type"]
    return code


# ---- success and the cache ----


@pytest.mark.endpoint("size_chart_read_create", case="success")
async def test_read_enqueues_one_job_and_the_suggestion_follows(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    _, p, image = _world(images_db, store)
    before = images_db.conn.execute(
        "SELECT attributes, provenance FROM products WHERE id = %s", [p]
    ).fetchone()
    response = await _read(api, images_db, p)
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["image_id"] == str(image) and body["status"] == "queued"
    assert body["poll_url"] == f"/api/v1/jobs/{body['job_id']}"
    assert _jobs(images_db, image) == 1
    queued = (await _detail(api, images_db, p))["size_chart_suggestion"]
    assert queued == {
        "image_id": str(image),
        "status": "queued",
        "chart": None,
        "confidence": None,
        "read_at": None,
    }
    await run_image_job(uuid.UUID(body["job_id"]))
    suggestion = (await _detail(api, images_db, p))["size_chart_suggestion"]
    assert suggestion["status"] == "read" and suggestion["confidence"] == 0.9
    assert [r["size"] for r in suggestion["chart"]["rows"]] == ["S", "M", "L"]
    assert suggestion["read_at"] is not None
    after = images_db.conn.execute(
        "SELECT attributes, provenance FROM products WHERE id = %s", [p]
    ).fetchone()
    assert after == before  # the read is never a fact
    assert (await _detail(api, images_db, p))["size_chart"] is None


async def test_live_job_answers_again(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    _, p, image = _world(images_db, store)
    first = await _read(api, images_db, p)
    second = await _read(api, images_db, p)
    assert (first.status_code, second.status_code) == (202, 202)
    assert second.json()["job_id"] == first.json()["job_id"]
    assert _jobs(images_db, image) == 1


@pytest.mark.parametrize("status", ["read", "unreadable"])
async def test_settled_result_answers_200_without_a_job(
    images_db: JobWorld,
    api: httpx.AsyncClient,
    store: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    tenant, p, image = _world(images_db, store)
    if status == "unreadable":
        from app.workers import size_chart_seam
        from mkos_pipeline import read_size_chart as stub

        async def unreadable(request: Any, credentials: Any) -> Any:
            got = await stub(request, credentials)
            return got.model_copy(update={"chart": None, "unreadable": True})

        monkeypatch.setattr(size_chart_seam, "call_read_size_chart", unreadable)
    first = await _read(api, images_db, p)
    await run_image_job(uuid.UUID(first.json()["job_id"]))
    runs = _runs(images_db, tenant)
    again = await _read(api, images_db, p)
    assert again.status_code == 200, again.text
    assert again.json() == {
        "image_id": str(image),
        "status": status,
        "job_id": None,
        "poll_url": None,
    }
    assert _jobs(images_db, image) == 1 and _runs(images_db, tenant) == runs


async def test_same_key_replays_the_first_202(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    _, p, _image = _world(images_db, store)
    key = str(uuid.uuid4())
    first = await _read(api, images_db, p, key=key)
    await run_image_job(uuid.UUID(first.json()["job_id"]))
    replay = await _read(api, images_db, p, key=key)
    assert replay.status_code == 202 and replay.json() == first.json()


# a same-key retry answers the stored first 202 whatever happened to the photos or the product
# since (invariant 10; the peek runs before the product and size-chart checks)


async def test_replay_after_the_photo_is_deleted_answers_the_first_202(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    _, p, image = _world(images_db, store)
    key = str(uuid.uuid4())
    first = await _read(api, images_db, p, key=key)
    assert first.status_code == 202, first.text
    images_db.conn.execute("UPDATE images SET deleted_at = now() WHERE id = %s", [image])
    replay = await _read(api, images_db, p, key=key)
    assert replay.status_code == 202, replay.text
    assert replay.json() == first.json()


async def test_replay_after_a_new_unchecked_photo_answers_the_first_202(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    tenant, p, _image = _world(images_db, store)
    key = str(uuid.uuid4())
    first = await _read(api, images_db, p, key=key)
    assert first.status_code == 202, first.text
    chart_image(images_db.conn, store, tenant, p, derived=False)  # newest, still being checked
    replay = await _read(api, images_db, p, key=key)
    assert replay.status_code == 202, replay.text
    assert replay.json() == first.json()


@pytest.mark.parametrize("change", ["deleted", "variant"])
async def test_replay_after_the_product_changes_answers_the_first_202(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage, change: str
) -> None:
    tenant, p, _image = _world(images_db, store)
    key = str(uuid.uuid4())
    first = await _read(api, images_db, p, key=key)
    assert first.status_code == 202, first.text
    if change == "deleted":
        images_db.conn.execute("UPDATE products SET deleted_at = now() WHERE id = %s", [p])
    else:  # p becomes the variant in the style's free `m` cell
        style, _child = style_and_child(images_db.conn, tenant)
        with images_db.conn.transaction():
            images_db.conn.execute(
                "UPDATE products SET parent_product_id = %s, category = 'kurtas', "
                "variant_attributes = %s, attributes = %s WHERE id = %s",
                [
                    style,
                    Jsonb({"size": "m"}),
                    Jsonb({"size": f.fact(value="M", normalized="m")}),
                    p,
                ],
            )
            images_db.conn.execute(
                "INSERT INTO variant_matrix_cells (tenant_id, style_product_id, colourway_id, "
                "size_key, availability, unavailable_reason, variant_product_id, source) "
                "VALUES (%s, %s, NULL, 'm', 'available', NULL, %s, 'seller')",
                [tenant, style, p],
            )
        assert (await _read(api, images_db, p)).status_code == 422  # a fresh key sees the variant
    replay = await _read(api, images_db, p, key=key)
    assert replay.status_code == 202, replay.text
    assert replay.json() == first.json()


@pytest.mark.parametrize("stored", ["unavailable", "failed"])
async def test_unsettled_result_asks_again(
    images_db: JobWorld,
    api: httpx.AsyncClient,
    store: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
    stored: str,
) -> None:
    import mkos_pipeline

    _, p, image = _world(images_db, store)
    if stored == "unavailable":
        monkeypatch.delattr(mkos_pipeline, "read_size_chart")
    else:
        from app.workers import size_chart_seam

        async def boom(request: Any, credentials: Any) -> Any:
            raise TimeoutError

        monkeypatch.setattr(size_chart_seam, "call_read_size_chart", boom)
    first = await _read(api, images_db, p)
    await run_image_job(uuid.UUID(first.json()["job_id"]), max_runs=5)
    assert read_state(images_db.conn, image)["status"] == stored  # type: ignore[index]
    assert (await _detail(api, images_db, p))["size_chart_suggestion"]["status"] == stored
    monkeypatch.undo()  # the pipeline ships `read_size_chart` again (AC7)
    use_read_storage(monkeypatch, store)
    again = await _read(api, images_db, p)
    assert again.status_code == 202, again.text
    assert again.json()["job_id"] != first.json()["job_id"]
    await run_image_job(uuid.UUID(again.json()["job_id"]))
    assert read_state(images_db.conn, image)["status"] == "read"  # type: ignore[index]


async def test_parallel_asks_make_one_job(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    tenant, p, image = _world(images_db, store)
    answers = await asyncio.gather(*(_read(api, images_db, p) for _ in range(4)))
    assert [a.status_code for a in answers] == [202] * 4, [a.text for a in answers]
    assert len({a.json()["job_id"] for a in answers}) == 1
    assert _jobs(images_db, image) == 1
    await run_image_job(uuid.UUID(answers[0].json()["job_id"]))
    assert _runs(images_db, tenant) <= 1


async def test_new_upload_hides_the_old_result(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    tenant, p, image = _world(images_db, store)
    first = await _read(api, images_db, p)
    await run_image_job(uuid.UUID(first.json()["job_id"]))
    newer, _ = chart_image(images_db.conn, store, tenant, p)
    suggestion = (await _detail(api, images_db, p))["size_chart_suggestion"]
    assert suggestion is None or suggestion["image_id"] == str(newer)
    assert suggestion is None or suggestion["status"] != "read"
    assert read_state(images_db.conn, image)["status"] == "read"  # type: ignore[index]


# ---- refusals ----


@pytest.mark.endpoint("size_chart_read_create", case="validation")
async def test_variant_no_chart_and_bad_key(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    tenant = images_db.tenant("acme")
    style, child = style_and_child(images_db.conn, tenant)
    chart_image(images_db.conn, store, tenant, style)
    response = await _read(api, images_db, child)
    assert response.status_code == 422 and _code(response) == "FACT_LEVEL_INVALID"
    bare = f.product(images_db.conn, tenant)
    response = await _read(api, images_db, bare)
    assert response.status_code == 422 and _code(response) == "VALIDATION_FAILED"
    hdrs = headers(images_db)
    hdrs.pop("Idempotency-Key")
    response = await api.post(_url(bare), headers=hdrs)
    assert response.status_code in (400, 422)
    response = await api.post(f"{PREFIX}/not-a-uuid/size-chart-read", headers=headers(images_db))
    assert response.status_code == 422


@pytest.mark.endpoint("size_chart_read_create", case="conflict")
async def test_no_derivative_409_and_key_reuse(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    tenant = images_db.tenant("acme")
    p = f.product(images_db.conn, tenant)
    chart_image(images_db.conn, store, tenant, p, derived=False)
    response = await _read(api, images_db, p)
    assert response.status_code == 409 and _code(response) == "INVALID_STATUS_TRANSITION"
    _, q, _image = _world(images_db, store)
    key = str(uuid.uuid4())
    assert (await _read(api, images_db, q, key=key)).status_code == 202
    other = f.product(images_db.conn, tenant)
    chart_image(images_db.conn, store, tenant, other)
    reused = await _read(api, images_db, other, key=key)
    assert reused.status_code == 409 and _code(reused) == "IDEMPOTENCY_KEY_REUSED"


async def test_ai_paused_503_writes_nothing(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    _, p, image = _world(images_db, store)
    images_db.conn.execute(
        "UPDATE ai_credit_state SET paused_reason = 'credit_exhausted', paused_at = now() "
        "WHERE id = 1"
    )
    try:
        response = await _read(api, images_db, p)
    finally:
        images_db.conn.execute(
            "UPDATE ai_credit_state SET paused_reason = NULL, paused_at = NULL WHERE id = 1"
        )
    assert response.status_code == 503 and _code(response) == "AI_CREDIT_EXHAUSTED"
    assert _jobs(images_db, image) == 0
    assert count(images_db.conn, "SELECT count(*) FROM idempotency_keys") == 0


async def test_daily_cap_429_writes_nothing(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    tenant, p, image = _world(images_db, store)
    images_db.conn.execute("UPDATE tenants SET llm_daily_budget_usd = 1 WHERE id = %s", [tenant])
    f.agent_run(images_db.conn, tenant, cost_usd=Decimal("0.99"), started_at=datetime.now(UTC))
    response = await _read(api, images_db, p)
    assert response.status_code == 429 and _code(response) == "BUDGET_EXCEEDED"
    assert "Retry-After" in response.headers
    assert _jobs(images_db, image) == 0


@pytest.mark.endpoint("size_chart_read_create", case="wrong_role")
async def test_reviewer_403_operator_202(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    _, p, _image = _world(images_db, store)
    assert (await _read(api, images_db, p, email="reviewer@acme.test")).status_code == 403
    assert (await _read(api, images_db, p, email="owner@acme.test")).status_code == 202


@pytest.mark.endpoint("size_chart_read_create", case="not_found")
async def test_unknown_product_404(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    response = await _read(api, images_db, uuid.uuid4())
    assert response.status_code == 404 and _code(response) == "NOT_FOUND"


@pytest.mark.endpoint("size_chart_read_create", case="wrong_tenant")
async def test_other_tenants_product_404_like_a_missing_one(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    rival = images_db.tenant("bharat")
    theirs = f.product(images_db.conn, rival)
    image, _ = chart_image(images_db.conn, store, rival, theirs)
    foreign = await _read(api, images_db, theirs)
    missing = await _read(api, images_db, uuid.uuid4())
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["error"]["message"] == missing.json()["error"]["message"]
    assert _jobs(images_db, image) == 0


@pytest.mark.endpoint("size_chart_read_create", case="missing_auth")
async def test_missing_auth(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    response = await api.post(_url(uuid.uuid4()), headers={"Idempotency-Key": str(uuid.uuid4())})
    assert response.status_code == 401


@pytest.mark.endpoint("size_chart_read_create", case="invalid_token", variant="expired")
@pytest.mark.endpoint("size_chart_read_create", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("size_chart_read_create", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_invalid_token(images_db: JobWorld, api: httpx.AsyncClient, variant: str) -> None:
    hdrs = {**bad_token(images_db, variant), "Idempotency-Key": str(uuid.uuid4())}
    response = await api.post(_url(uuid.uuid4()), headers=hdrs)
    assert response.status_code == 401


# ---- the suggestion on reads ----


async def test_suggestion_null_on_variants_lists_and_without_a_chart(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    tenant = images_db.tenant("acme")
    style, child = style_and_child(images_db.conn, tenant)
    chart_image(images_db.conn, store, tenant, style)
    assert (await _detail(api, images_db, child))["size_chart_suggestion"] is None
    bare = f.product(images_db.conn, tenant)
    assert (await _detail(api, images_db, bare))["size_chart_suggestion"] is None
    _, p, _image = _world(images_db, store)
    first = await _read(api, images_db, p)
    await run_image_job(uuid.UUID(first.json()["job_id"]))
    listed = await api.get(
        f"{PREFIX}?limit=100", headers=as_member(images_db, "reviewer@acme.test")
    )
    item = next(x for x in listed.json()["data"] if x["id"] == str(p))
    assert item["size_chart_suggestion"] is None
    assert job_row(images_db.conn, uuid.UUID(first.json()["job_id"]))["status"] == "succeeded"
