"""SI14 T16 (AC7-AC8): the column-map ops as app_rw: create, read (with the dead-job settle) and
confirm, their refusals and the header echo safety."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from .assist_support import set_switch
from .conftest import LOCAL_HOSTS
from .jobs_support import JobWorld, as_member, bad_token, count, jobs_db

__all__ = ["jobs_db"]
pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
MAPS = "/api/v1/imports/column-maps"
OPERATOR = "operator@acme.test"
REQUIRED = ["Item SKU", "product_name", "category"]


def _hdrs(jw: JobWorld, email: str = OPERATOR, key: str | None = None) -> dict[str, str]:
    return {**as_member(jw, email), "Idempotency-Key": key or str(uuid.uuid4())}


async def _create(
    api: httpx.AsyncClient,
    jw: JobWorld,
    headers: list[str],
    *,
    samples: dict[str, list[str]] | None = None,
    email: str = OPERATOR,
    key: str | None = None,
) -> httpx.Response:
    body: dict[str, Any] = {"headers": headers}
    if samples is not None:
        body["samples"] = samples
    return await api.post(MAPS, headers=_hdrs(jw, email, key), json=body)


async def _confirm(
    api: httpx.AsyncClient,
    jw: JobWorld,
    map_id: str,
    answers: list[dict[str, Any]],
    *,
    email: str = OPERATOR,
    key: str | None = None,
) -> httpx.Response:
    return await api.post(
        f"{MAPS}/{map_id}/confirm", headers=_hdrs(jw, email, key), json={"mappings": answers}
    )


def _code(response: httpx.Response) -> str:
    return str(response.json()["error"]["type"])


def _jobs(jw: JobWorld) -> int:
    return count(jw.conn, "SELECT count(*) FROM job_runs WHERE entity_type = 'column_map'")


def _samples(jw: JobWorld, map_id: str) -> Any:
    row = jw.conn.execute("SELECT samples FROM import_column_maps WHERE id = %s", [map_id])
    return row.fetchone()[0]  # type: ignore[index]


@pytest.fixture(autouse=True)
def _ai_on(monkeypatch: pytest.MonkeyPatch) -> None:
    set_switch(monkeypatch, "columns", on=True)


# ---- create ----


@pytest.mark.endpoint("imports_column_map_create", case="success")
async def test_no_leftovers_is_ready_with_no_job(jobs_db: JobWorld, api: httpx.AsyncClient) -> None:
    response = await _create(api, jobs_db, [*REQUIRED, "Colour"], samples={"Colour": ["red"]})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "ready" and body["needs_review_count"] == 0
    assert [m["key"] for m in body["mappings"]] == [
        "seller_sku",
        "product_name",
        "category",
        "color_name",
    ]
    assert _jobs(jobs_db) == 0 and _samples(jobs_db, body["id"]) is None


async def test_leftovers_with_the_ai_on_queue_one_job(
    jobs_db: JobWorld, api: httpx.AsyncClient
) -> None:
    response = await _create(
        api, jobs_db, [*REQUIRED, "Warehouse Bin"], samples={"Warehouse Bin": ["B-12"]}
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "mapping" and body["needs_review_count"] == 1
    assert _jobs(jobs_db) == 1
    assert _samples(jobs_db, body["id"]) is None  # no reader yet: seller cells are not stored


async def test_leftovers_with_the_ai_off_are_ready_for_the_seller(
    jobs_db: JobWorld, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_switch(monkeypatch, "columns", on=False)
    response = await _create(api, jobs_db, [*REQUIRED, "Warehouse Bin"])
    assert response.status_code == 201 and response.json()["status"] == "ready"
    assert response.json()["mappings"][3]["needs_review"] is True and _jobs(jobs_db) == 0


async def test_ai_paused_still_maps_without_it(jobs_db: JobWorld, api: httpx.AsyncClient) -> None:
    jobs_db.conn.execute(
        "UPDATE ai_credit_state SET paused_reason = 'credit_exhausted', paused_at = now() "
        "WHERE id = 1"
    )
    try:
        response = await _create(api, jobs_db, [*REQUIRED, "Warehouse Bin"])
    finally:
        jobs_db.conn.execute(
            "UPDATE ai_credit_state SET paused_reason = NULL, paused_at = NULL WHERE id = 1"
        )
    assert response.status_code == 201 and response.json()["status"] == "ready"
    assert _jobs(jobs_db) == 0


@pytest.mark.endpoint("imports_column_map_create", case="conflict")
async def test_replay_and_key_reuse(jobs_db: JobWorld, api: httpx.AsyncClient) -> None:
    key = str(uuid.uuid4())
    first = await _create(api, jobs_db, REQUIRED, key=key)
    again = await _create(api, jobs_db, REQUIRED, key=key)
    assert first.status_code == again.status_code == 201 and first.json() == again.json()
    other = await _create(api, jobs_db, [*REQUIRED, "brand"], key=key)
    assert other.status_code == 409 and _code(other) == "IDEMPOTENCY_KEY_REUSED"


@pytest.mark.endpoint("imports_column_map_create", case="validation")
async def test_create_refusals(jobs_db: JobWorld, api: httpx.AsyncClient) -> None:
    duplicate = await _create(api, jobs_db, ["SKU", "sku "])
    assert duplicate.status_code == 422 and _code(duplicate) == "VALIDATION_FAILED"
    too_many = await _create(api, jobs_db, [f"h{n}" for n in range(201)])
    assert too_many.status_code == 422
    assert (await _create(api, jobs_db, [])).status_code == 422


@pytest.mark.endpoint("imports_column_map_create", case="wrong_role")
async def test_reviewer_403_and_owner_ok(jobs_db: JobWorld, api: httpx.AsyncClient) -> None:
    assert (await _create(api, jobs_db, REQUIRED, email="reviewer@acme.test")).status_code == 403
    assert (await _create(api, jobs_db, REQUIRED, email="owner@acme.test")).status_code == 201


@pytest.mark.endpoint("imports_column_map_create", case="wrong_tenant")
async def test_a_map_is_its_tenants_own(jobs_db: JobWorld, api: httpx.AsyncClient) -> None:
    theirs = (await _create(api, jobs_db, REQUIRED, email="operator@bharat.test")).json()["id"]
    read = await api.get(f"{MAPS}/{theirs}", headers=as_member(jobs_db, OPERATOR))
    assert read.status_code == 404


@pytest.mark.endpoint("imports_column_map_create", case="missing_auth")
async def test_create_missing_auth(jobs_db: JobWorld, api: httpx.AsyncClient) -> None:
    response = await api.post(
        MAPS, headers={"Idempotency-Key": str(uuid.uuid4())}, json={"headers": REQUIRED}
    )
    assert response.status_code == 401


@pytest.mark.endpoint("imports_column_map_create", case="invalid_token", variant="expired")
@pytest.mark.endpoint("imports_column_map_create", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("imports_column_map_create", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_create_invalid_token(
    jobs_db: JobWorld, api: httpx.AsyncClient, variant: str
) -> None:
    hdrs = {**bad_token(jobs_db, variant), "Idempotency-Key": str(uuid.uuid4())}
    assert (await api.post(MAPS, headers=hdrs, json={"headers": REQUIRED})).status_code == 401


# ---- read ----


@pytest.mark.endpoint("imports_column_map_get", case="success")
async def test_any_member_reads_a_map(jobs_db: JobWorld, api: httpx.AsyncClient) -> None:
    made = (await _create(api, jobs_db, REQUIRED)).json()
    read = await api.get(f"{MAPS}/{made['id']}", headers=as_member(jobs_db, "reviewer@acme.test"))
    assert read.status_code == 200 and read.json() == made


@pytest.mark.endpoint("imports_column_map_get", case="validation")
async def test_get_bad_id_422(jobs_db: JobWorld, api: httpx.AsyncClient) -> None:
    response = await api.get(f"{MAPS}/not-a-uuid", headers=as_member(jobs_db, OPERATOR))
    assert response.status_code == 422


@pytest.mark.endpoint("imports_column_map_get", case="not_found")
async def test_get_unknown_404(jobs_db: JobWorld, api: httpx.AsyncClient) -> None:
    response = await api.get(f"{MAPS}/{uuid.uuid4()}", headers=as_member(jobs_db, OPERATOR))
    assert response.status_code == 404 and _code(response) == "NOT_FOUND"


@pytest.mark.endpoint("imports_column_map_get", case="wrong_tenant")
async def test_get_foreign_404_like_missing(jobs_db: JobWorld, api: httpx.AsyncClient) -> None:
    theirs = (await _create(api, jobs_db, REQUIRED, email="operator@bharat.test")).json()["id"]
    foreign = await api.get(f"{MAPS}/{theirs}", headers=as_member(jobs_db, OPERATOR))
    missing = await api.get(f"{MAPS}/{uuid.uuid4()}", headers=as_member(jobs_db, OPERATOR))
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["error"]["message"] == missing.json()["error"]["message"]


@pytest.mark.endpoint("imports_column_map_get", case="missing_auth")
async def test_get_missing_auth(jobs_db: JobWorld, api: httpx.AsyncClient) -> None:
    assert (await api.get(f"{MAPS}/{uuid.uuid4()}")).status_code == 401


@pytest.mark.endpoint("imports_column_map_get", case="invalid_token", variant="expired")
@pytest.mark.endpoint("imports_column_map_get", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("imports_column_map_get", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_get_invalid_token(jobs_db: JobWorld, api: httpx.AsyncClient, variant: str) -> None:
    response = await api.get(f"{MAPS}/{uuid.uuid4()}", headers=bad_token(jobs_db, variant))
    assert response.status_code == 401


async def test_a_dead_job_settles_the_map_failed(jobs_db: JobWorld, api: httpx.AsyncClient) -> None:
    made = (
        await _create(
            api, jobs_db, [*REQUIRED, "Warehouse Bin"], samples={"Warehouse Bin": ["B-12"]}
        )
    ).json()
    assert made["status"] == "mapping"
    jobs_db.conn.execute(
        "UPDATE job_runs SET status = 'cancelled', finished_at = now() "
        "WHERE entity_type = 'column_map' AND entity_id = %s",
        [made["id"]],
    )
    read = await api.get(f"{MAPS}/{made['id']}", headers=as_member(jobs_db, OPERATOR))
    assert read.status_code == 200 and read.json()["status"] == "failed"
    assert [m["key"] for m in read.json()["mappings"][:3]] == [
        "seller_sku",
        "product_name",
        "category",
    ]
    confirmed = await _confirm(api, jobs_db, made["id"], [{"position": 4, "key": None}])
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "confirmed" and _samples(jobs_db, made["id"]) is None


# ---- confirm ----


@pytest.mark.endpoint("imports_column_map_confirm", case="success")
async def test_confirm_answers_by_position(
    jobs_db: JobWorld, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_switch(monkeypatch, "columns", on=False)
    made = (await _create(api, jobs_db, [*REQUIRED, "Warehouse Bin", "Shade"])).json()
    response = await _confirm(api, jobs_db, made["id"], [{"position": 4, "key": "style_number"}])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "confirmed" and body["needs_review_count"] == 0
    assert body["mappings"][3]["key"] == "style_number"
    assert body["mappings"][3]["method"] == "none" and body["mappings"][3]["confidence"] == 1.0
    again = await _confirm(api, jobs_db, made["id"], [{"position": 4, "key": "style_number"}])
    assert again.status_code == 409 and _code(again) == "INVALID_STATUS_TRANSITION"


@pytest.mark.endpoint("imports_column_map_confirm", case="validation")
async def test_confirm_refusals(
    jobs_db: JobWorld, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_switch(monkeypatch, "columns", on=False)
    made = (await _create(api, jobs_db, [*REQUIRED, "Warehouse Bin", "Shade"])).json()
    map_id = made["id"]
    unanswered = await _confirm(api, jobs_db, map_id, [{"position": 5, "key": "color_name"}])
    assert unanswered.status_code == 422 and _code(unanswered) == "VALIDATION_FAILED"
    unknown = await _confirm(api, jobs_db, map_id, [{"position": 4, "key": "not_a_column"}])
    assert unknown.status_code == 422
    duplicate = await _confirm(api, jobs_db, map_id, [{"position": 4, "key": "color_name"}])
    assert duplicate.status_code == 422  # position 5 already holds color_name
    absent = await _confirm(
        api, jobs_db, map_id, [{"position": 4, "key": None}, {"position": 9, "key": None}]
    )
    assert absent.status_code == 422
    bullet_onto_other = await _confirm(api, jobs_db, map_id, [{"position": 4, "key": "bullet_1"}])
    assert bullet_onto_other.status_code == 422
    assert (await _confirm(api, jobs_db, map_id, [{"position": 0, "key": None}])).status_code == 422


async def test_confirm_without_a_required_column_is_import_failed(
    jobs_db: JobWorld, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_switch(monkeypatch, "columns", on=False)
    made = (await _create(api, jobs_db, ["Item SKU", "product_name", "Warehouse Bin"])).json()
    response = await _confirm(api, jobs_db, made["id"], [{"position": 3, "key": None}])
    assert response.status_code == 422 and _code(response) == "IMPORT_FAILED"
    report = response.json()["error"]["error_report"]
    assert [e["field"] for e in report["errors"]] == ["file.header"]
    assert "category" in report["errors"][0]["message"]


@pytest.mark.endpoint("imports_column_map_confirm", case="conflict")
async def test_confirm_while_mapping_409(jobs_db: JobWorld, api: httpx.AsyncClient) -> None:
    made = (await _create(api, jobs_db, [*REQUIRED, "Warehouse Bin"])).json()
    assert made["status"] == "mapping"
    response = await _confirm(api, jobs_db, made["id"], [{"position": 4, "key": None}])
    assert response.status_code == 409 and _code(response) == "INVALID_STATUS_TRANSITION"
    key = str(uuid.uuid4())
    ready = (await _create(api, jobs_db, REQUIRED)).json()
    assert (
        await _confirm(api, jobs_db, ready["id"], [{"position": 1, "key": "seller_sku"}], key=key)
    ).status_code == 200
    reused = await _confirm(
        api, jobs_db, ready["id"], [{"position": 1, "key": "style_number"}], key=key
    )
    assert reused.status_code == 409 and _code(reused) == "IDEMPOTENCY_KEY_REUSED"


@pytest.mark.endpoint("imports_column_map_confirm", case="wrong_role")
async def test_confirm_reviewer_403(jobs_db: JobWorld, api: httpx.AsyncClient) -> None:
    made = (await _create(api, jobs_db, REQUIRED)).json()
    response = await _confirm(
        api, jobs_db, made["id"], [{"position": 1, "key": "seller_sku"}], email="reviewer@acme.test"
    )
    assert response.status_code == 403


@pytest.mark.endpoint("imports_column_map_confirm", case="not_found")
async def test_confirm_unknown_404(jobs_db: JobWorld, api: httpx.AsyncClient) -> None:
    response = await _confirm(api, jobs_db, str(uuid.uuid4()), [{"position": 1, "key": None}])
    assert response.status_code == 404


@pytest.mark.endpoint("imports_column_map_confirm", case="wrong_tenant")
async def test_confirm_foreign_404(jobs_db: JobWorld, api: httpx.AsyncClient) -> None:
    theirs = (await _create(api, jobs_db, REQUIRED, email="operator@bharat.test")).json()["id"]
    response = await _confirm(api, jobs_db, theirs, [{"position": 1, "key": "seller_sku"}])
    assert response.status_code == 404


@pytest.mark.endpoint("imports_column_map_confirm", case="missing_auth")
async def test_confirm_missing_auth(jobs_db: JobWorld, api: httpx.AsyncClient) -> None:
    response = await api.post(
        f"{MAPS}/{uuid.uuid4()}/confirm",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={"mappings": [{"position": 1, "key": None}]},
    )
    assert response.status_code == 401


@pytest.mark.endpoint("imports_column_map_confirm", case="invalid_token", variant="expired")
@pytest.mark.endpoint("imports_column_map_confirm", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("imports_column_map_confirm", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_confirm_invalid_token(
    jobs_db: JobWorld, api: httpx.AsyncClient, variant: str
) -> None:
    hdrs = {**bad_token(jobs_db, variant), "Idempotency-Key": str(uuid.uuid4())}
    response = await api.post(
        f"{MAPS}/{uuid.uuid4()}/confirm",
        headers=hdrs,
        json={"mappings": [{"position": 1, "key": None}]},
    )
    assert response.status_code == 401


# ---- echo safety and bullets ----


async def test_crafted_headers_are_never_echoed_and_still_confirm(
    jobs_db: JobWorld, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_switch(monkeypatch, "columns", on=False)
    crafted = ['=HYPERLINK("x")', "<script>alert(1)</script>"]
    made = await _create(api, jobs_db, [*REQUIRED, *crafted])
    assert made.status_code == 201
    texts = [made.text]
    body = made.json()
    assert [m["header_shown"] for m in body["mappings"][3:]] == ["column 4", "column 5"]
    texts.append((await api.get(f"{MAPS}/{body['id']}", headers=as_member(jobs_db, OPERATOR))).text)
    bad = await _confirm(api, jobs_db, body["id"], [{"position": 4, "key": "oops"}])
    texts.append(bad.text)
    done = await _confirm(
        api, jobs_db, body["id"], [{"position": 4, "key": None}, {"position": 5, "key": None}]
    )
    assert done.status_code == 200
    texts.append(done.text)
    for text in texts:
        assert "HYPERLINK" not in text and "<script>" not in text and "script" not in text


async def test_bullets_confirm_as_three_columns(jobs_db: JobWorld, api: httpx.AsyncClient) -> None:
    made = (await _create(api, jobs_db, [*REQUIRED, "bullet_1", "bullet_2", "bullet_3"])).json()
    assert [m["key"] for m in made["mappings"][3:]] == ["bullet_1", "bullet_2", "bullet_3"]
    response = await _confirm(api, jobs_db, made["id"], [{"position": 4, "key": "bullet_1"}])
    assert response.status_code == 200 and response.json()["status"] == "confirmed"
