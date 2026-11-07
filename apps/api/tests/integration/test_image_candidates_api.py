"""SI7 T13-T14: the image candidate API as app_rw, on a fake object store and the stub pipeline.

Part 1 (T13): create and list."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx
import pytest
from app.workers import image_edit_seam

from . import factories as f
from .candidates_support import (
    candidate_row,
    chart_product,
    edit_on,
    photo,
    source_main,
    use_candidate_storage,
)
from .conftest import LOCAL_HOSTS
from .images_support import (
    FakeStorage,
    headers,
    image_row,
    images_db,
    product,
    run_image_job,
    style_and_child,
)
from .jobs_support import JobWorld, as_member, bad_token, count

__all__ = ["edit_on", "images_db"]
pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
PREFIX = "/api/v1/products"


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStorage:
    fake = FakeStorage()
    use_candidate_storage(monkeypatch, fake)
    return fake


def _url(product_id: Any) -> str:
    return f"{PREFIX}/{product_id}/image-candidates"


async def _create(
    api: httpx.AsyncClient,
    jw: JobWorld,
    product_id: Any,
    body: dict[str, Any],
    *,
    email: str = "operator@acme.test",
    key: str | None = None,
) -> httpx.Response:
    return await api.post(_url(product_id), json=body, headers=headers(jw, email, key))


def _jobs_for(jw: JobWorld, candidate_id: Any) -> int:
    return count(
        jw.conn,
        "SELECT count(*) FROM job_runs WHERE entity_id = %s",
        [uuid.UUID(str(candidate_id))],
    )


def _loc(response: httpx.Response) -> list[Any]:
    details = response.json()["error"]["details"]
    return list(details[0]["loc"])


@pytest.mark.endpoint("image_candidates_create", case="success")
async def test_create_fix_is_queued(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    p, src, _ = source_main(images_db.conn, store, images_db.tenant("acme"))
    response = await _create(api, images_db, p, {"kind": "fix", "source_image_id": str(src)})
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "queued" and body["poll_url"] == f"/api/v1/jobs/{body['job_id']}"
    row = candidate_row(images_db.conn, uuid.UUID(body["candidate_id"]))
    assert str(row["job_id"]) == body["job_id"] and row["kind"] == "fix"
    audit = count(
        images_db.conn,
        "SELECT count(*) FROM audit_log WHERE action = 'image_candidate.created' AND entity_id = %s",
        [row["id"]],
    )
    assert audit == 1


@pytest.mark.endpoint("image_candidates_create", case="conflict")
async def test_idempotent_replay_and_key_reuse(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    p, src, _ = source_main(images_db.conn, store, images_db.tenant("acme"))
    key = str(uuid.uuid4())
    body = {"kind": "fix", "source_image_id": str(src)}
    first = await _create(api, images_db, p, body, key=key)
    again = await _create(api, images_db, p, body, key=key)
    assert first.status_code == again.status_code == 202
    assert first.json() == again.json()
    other = await _create(api, images_db, p, {"kind": "size_chart"}, key=key)
    assert other.status_code == 409
    assert other.json()["error"]["type"] == "IDEMPOTENCY_KEY_REUSED"


async def test_second_create_with_a_new_key_is_the_same_candidate(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    p, src, _ = source_main(images_db.conn, store, images_db.tenant("acme"))
    body = {"kind": "fix", "source_image_id": str(src)}
    first = (await _create(api, images_db, p, body)).json()
    await run_image_job(uuid.UUID(first["job_id"]))
    second = await _create(api, images_db, p, body)
    assert second.status_code == 202
    answer = second.json()
    assert answer["candidate_id"] == first["candidate_id"]
    assert answer["status"] == "ready" and answer["job_id"] is None and answer["poll_url"] is None
    assert _jobs_for(images_db, first["candidate_id"]) == 1


async def test_second_create_while_queued_returns_the_running_job(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage, edit_on: pytest.MonkeyPatch
) -> None:
    from mkos_pipeline import edit_image as stub

    tenant = images_db.tenant("acme")
    p, src, _ = source_main(images_db.conn, store, tenant)
    body = {"kind": "white_background", "source_image_id": str(src)}
    first = (await _create(api, images_db, p, body)).json()
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(request: Any, credentials: Any) -> Any:
        entered.set()
        await release.wait()
        return await stub(request, credentials)

    edit_on.setattr(image_edit_seam, "call_edit_image", blocked)
    job = asyncio.create_task(run_image_job(uuid.UUID(first["job_id"])))
    await asyncio.wait_for(entered.wait(), 20)
    second = (await _create(api, images_db, p, body)).json()
    release.set()
    await job
    assert (second["candidate_id"], second["job_id"]) == (first["candidate_id"], first["job_id"])
    assert _jobs_for(images_db, first["candidate_id"]) == 1
    row = candidate_row(images_db.conn, uuid.UUID(first["candidate_id"]))
    assert row["status"] == "ready", row["failure_reason"]
    runs = count(images_db.conn, "SELECT count(*) FROM agent_runs WHERE tenant_id = %s", [tenant])
    assert runs == 1


async def test_concurrent_creates_make_one_row_and_one_job(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    p, src, _ = source_main(images_db.conn, store, images_db.tenant("acme"))
    body = {"kind": "fix", "source_image_id": str(src)}
    a, b = await asyncio.gather(_create(api, images_db, p, body), _create(api, images_db, p, body))
    assert a.status_code == b.status_code == 202
    assert a.json()["candidate_id"] == b.json()["candidate_id"]
    assert a.json()["job_id"] == b.json()["job_id"]
    assert _jobs_for(images_db, a.json()["candidate_id"]) == 1


async def test_same_photo_on_another_product_is_its_own_candidate(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    tenant = images_db.tenant("acme")
    data = photo()
    p1, src1, _ = source_main(images_db.conn, store, tenant, clean=data)
    p2, src2, _ = source_main(images_db.conn, store, tenant, clean=data)
    one = (await _create(api, images_db, p1, {"kind": "fix", "source_image_id": str(src1)})).json()
    two = (await _create(api, images_db, p2, {"kind": "fix", "source_image_id": str(src2)})).json()
    assert one["candidate_id"] != two["candidate_id"]


async def test_another_tenant_gets_its_own_row(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    data = photo()
    pa, sa, _ = source_main(images_db.conn, store, images_db.tenant("acme"), clean=data)
    pb, sb, _ = source_main(images_db.conn, store, images_db.tenant("bharat"), clean=data)
    a = await _create(api, images_db, pa, {"kind": "fix", "source_image_id": str(sa)})
    b = await _create(
        api,
        images_db,
        pb,
        {"kind": "fix", "source_image_id": str(sb)},
        email="operator@bharat.test",
    )
    assert a.json()["candidate_id"] != b.json()["candidate_id"]


@pytest.mark.endpoint("image_candidates_create", case="validation")
@pytest.mark.parametrize(
    "body",
    [{"kind": "fix"}, {"kind": "size_chart", "source_image_id": str(uuid.uuid4())}, {"kind": "x"}],
    ids=["fix-no-source", "chart-with-source", "unknown-kind"],
)
async def test_bad_bodies_422(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage, body: dict[str, Any]
) -> None:
    p = product(images_db.conn, images_db.tenant("acme"))
    response = await _create(api, images_db, p, body)
    assert response.status_code == 422


async def test_switch_off_is_422_at_kind(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    p, src, _ = source_main(images_db.conn, store, images_db.tenant("acme"))
    body = {"kind": "white_background", "source_image_id": str(src)}
    response = await _create(api, images_db, p, body)
    assert response.status_code == 422 and _loc(response) == ["body", "kind"]


@pytest.mark.endpoint("image_candidates_create", case="not_found")
async def test_unknown_product_and_source_404(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    tenant = images_db.tenant("acme")
    p, src, _ = source_main(images_db.conn, store, tenant)
    unknown = await _create(
        api, images_db, uuid.uuid4(), {"kind": "fix", "source_image_id": str(src)}
    )
    assert unknown.status_code == 404
    missing = await _create(
        api, images_db, p, {"kind": "fix", "source_image_id": str(uuid.uuid4())}
    )
    assert missing.status_code == 404


@pytest.mark.parametrize("case", ["secondary", "deleted", "other_product"])
async def test_source_must_be_this_products_live_main(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage, case: str
) -> None:
    from datetime import UTC, datetime

    tenant = images_db.tenant("acme")
    p, src, _ = source_main(images_db.conn, store, tenant)
    if case == "secondary":
        src = image_row(
            images_db.conn,
            tenant,
            p,
            slot="secondary",
            clean_content_hash="a" * 64,
            clean_mime_type="image/png",
        )
    elif case == "deleted":
        images_db.conn.execute(
            "UPDATE images SET deleted_at = %s WHERE id = %s", [datetime.now(UTC), src]
        )
    else:
        p = product(images_db.conn, tenant)
    response = await _create(api, images_db, p, {"kind": "fix", "source_image_id": str(src)})
    assert response.status_code == 404
    assert response.json()["error"]["message"] == "source_image_id not found."


async def test_source_without_a_clean_copy_is_422(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    tenant = images_db.tenant("acme")
    p = product(images_db.conn, tenant)
    src = image_row(images_db.conn, tenant, p, slot="main")
    response = await _create(api, images_db, p, {"kind": "fix", "source_image_id": str(src)})
    assert response.status_code == 422 and _loc(response) == ["body", "source_image_id"]


async def test_size_chart_refusals(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    tenant = images_db.tenant("acme")
    _, child = style_and_child(images_db.conn, tenant)
    variant = await _create(api, images_db, child, {"kind": "size_chart"})
    assert variant.status_code == 422 and _loc(variant) == ["body", "product_id"]
    bare = await _create(api, images_db, product(images_db.conn, tenant), {"kind": "size_chart"})
    assert bare.status_code == 422 and _loc(bare) == ["body", "kind"]
    chart = {"unit": "cm", "basis": "garment", "rows": [{"size": "चिकनकारी", "chest": 90}]}
    hindi = await _create(
        api, images_db, chart_product(images_db.conn, tenant, chart), {"kind": "size_chart"}
    )
    assert hindi.status_code == 422 and _loc(hindi) == ["body", "kind"]


async def test_size_chart_create_is_queued(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    p = chart_product(images_db.conn, images_db.tenant("acme"))
    response = await _create(api, images_db, p, {"kind": "size_chart"})
    assert response.status_code == 202 and response.json()["status"] == "queued"
    await run_image_job(uuid.UUID(response.json()["job_id"]))
    row = candidate_row(images_db.conn, uuid.UUID(response.json()["candidate_id"]))
    assert row["status"] == "ready"


@pytest.mark.endpoint("image_candidates_create", case="wrong_role")
async def test_reviewer_cannot_create(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    p, src, _ = source_main(images_db.conn, store, images_db.tenant("acme"))
    response = await _create(
        api, images_db, p, {"kind": "fix", "source_image_id": str(src)}, email="reviewer@acme.test"
    )
    assert response.status_code == 403


@pytest.mark.endpoint("image_candidates_create", case="wrong_tenant")
async def test_other_tenants_product_or_source_is_404(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    pb, sb, _ = source_main(images_db.conn, store, images_db.tenant("bharat"))
    response = await _create(api, images_db, pb, {"kind": "fix", "source_image_id": str(sb)})
    assert response.status_code == 404
    pa = product(images_db.conn, images_db.tenant("acme"))
    response = await _create(api, images_db, pa, {"kind": "fix", "source_image_id": str(sb)})
    assert response.status_code == 404
    assert count(images_db.conn, "SELECT count(*) FROM image_candidates") == 0


@pytest.mark.endpoint("image_candidates_create", case="missing_auth")
async def test_create_missing_auth(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    response = await api.post(_url(uuid.uuid4()), json={"kind": "size_chart"})
    assert response.status_code == 401


@pytest.mark.endpoint("image_candidates_create", case="invalid_token", variant="expired")
@pytest.mark.endpoint("image_candidates_create", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("image_candidates_create", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_create_invalid_token(
    images_db: JobWorld, api: httpx.AsyncClient, variant: str
) -> None:
    hdrs = {**bad_token(images_db, variant), "Idempotency-Key": str(uuid.uuid4())}
    response = await api.post(_url(uuid.uuid4()), json={"kind": "size_chart"}, headers=hdrs)
    assert response.status_code == 401


async def test_live_image_job_cap_is_429(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    from .images_support import enqueue_image_job

    tenant = images_db.tenant("acme")
    p, src, _ = source_main(images_db.conn, store, tenant)
    for _ in range(20):
        await enqueue_image_job(tenant, uuid.uuid4())
    response = await _create(api, images_db, p, {"kind": "fix", "source_image_id": str(src)})
    assert response.status_code == 429


@pytest.mark.endpoint("image_candidates_list", case="success")
async def test_list_newest_first_with_previews_for_stored_rows(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    from datetime import UTC, datetime, timedelta

    tenant = images_db.tenant("acme")
    p, src, _ = source_main(images_db.conn, store, tenant)
    now = datetime.now(UTC)
    ready = f.image_candidate(
        images_db.conn, tenant, p, src, status="ready", storage_key="k-ready", created_at=now
    )
    queued = f.image_candidate(
        images_db.conn, tenant, p, src, created_at=now - timedelta(minutes=1)
    )
    response = await api.get(_url(p), headers=as_member(images_db, "reviewer@acme.test"))
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [i["id"] for i in items] == [str(ready), str(queued)]
    assert "X-Amz-Signature" in items[0]["preview_url"]
    assert items[1]["preview_url"] is None
    assert all("X-Amz-Signature" in i["source_preview_url"] for i in items)


async def test_list_holds_at_most_fifty(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    tenant = images_db.tenant("acme")
    p, src, _ = source_main(images_db.conn, store, tenant)
    for _ in range(55):
        f.image_candidate(images_db.conn, tenant, p, src)
    response = await api.get(_url(p), headers=as_member(images_db, "operator@acme.test"))
    assert len(response.json()["items"]) == 50


@pytest.mark.endpoint("image_candidates_list", case="validation")
async def test_list_bad_id_422(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    response = await api.get(_url("not-a-uuid"), headers=as_member(images_db, "operator@acme.test"))
    assert response.status_code == 422


@pytest.mark.endpoint("image_candidates_list", case="not_found")
async def test_list_unknown_product_404(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    response = await api.get(_url(uuid.uuid4()), headers=as_member(images_db, "operator@acme.test"))
    assert response.status_code == 404


@pytest.mark.endpoint("image_candidates_list", case="wrong_tenant")
async def test_list_other_tenant_404(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    pb = product(images_db.conn, images_db.tenant("bharat"))
    response = await api.get(_url(pb), headers=as_member(images_db, "operator@acme.test"))
    assert response.status_code == 404


@pytest.mark.endpoint("image_candidates_list", case="missing_auth")
async def test_list_missing_auth(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    assert (await api.get(_url(uuid.uuid4()))).status_code == 401


@pytest.mark.endpoint("image_candidates_list", case="invalid_token", variant="expired")
@pytest.mark.endpoint("image_candidates_list", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("image_candidates_list", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_list_invalid_token(
    images_db: JobWorld, api: httpx.AsyncClient, variant: str
) -> None:
    response = await api.get(_url(uuid.uuid4()), headers=bad_token(images_db, variant))
    assert response.status_code == 401
