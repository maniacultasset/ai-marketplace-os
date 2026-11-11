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


# ---- Part 2 (T14): accept and reject ----

CANDIDATES = "/api/v1/image-candidates"


async def _ready(
    api: httpx.AsyncClient,
    jw: JobWorld,
    store: FakeStorage,
    *,
    kind: str = "fix",
    slug: str = "acme",
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """A ready candidate made through the API and the real job: (candidate, product, source)."""
    tenant = jw.tenant(slug)
    if kind == "size_chart":
        p, src = chart_product(jw.conn, tenant), None
        body: dict[str, Any] = {"kind": "size_chart"}
    else:
        p, src, _ = source_main(jw.conn, store, tenant)
        body = {"kind": kind, "source_image_id": str(src)}
    made = await _create(api, jw, p, body, email=f"operator@{slug}.test")
    assert made.status_code == 202, made.text
    await run_image_job(uuid.UUID(made.json()["job_id"]))
    candidate = uuid.UUID(made.json()["candidate_id"])
    assert candidate_row(jw.conn, candidate)["status"] == "ready"
    return candidate, p, src  # type: ignore[return-value]


async def _decide(
    api: httpx.AsyncClient,
    jw: JobWorld,
    candidate: Any,
    action: str,
    *,
    email: str = "operator@acme.test",
    key: str | None = None,
) -> httpx.Response:
    return await api.post(f"{CANDIDATES}/{candidate}/{action}", headers=headers(jw, email, key))


def _images(jw: JobWorld, product_id: uuid.UUID) -> list[dict[str, Any]]:
    cur = jw.conn.execute(
        "SELECT id, slot, position, origin, parent_image_id, candidate_id, deleted_at "
        "FROM images WHERE product_id = %s ORDER BY created_at",
        [product_id],
    )
    names = [d.name for d in cur.description or ()]
    return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]


@pytest.mark.endpoint("image_candidates_accept", case="success")
async def test_accept_white_background_makes_the_new_main(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage, edit_on: pytest.MonkeyPatch
) -> None:
    candidate, p, src = await _ready(api, images_db, store, kind="white_background")
    response = await _decide(api, images_db, candidate, "accept")
    assert response.status_code == 202, response.text
    body = response.json()
    new_id = uuid.UUID(body["image_id"])
    rows = {r["id"]: r for r in _images(images_db, p)}
    new, old = rows[new_id], rows[src]
    assert (new["slot"], new["origin"], new["parent_image_id"], new["candidate_id"]) == (
        "main",
        "generated",
        src,
        candidate,
    )
    assert old["slot"] == "secondary" and old["deleted_at"] is None
    live_mains = [r for r in rows.values() if r["slot"] == "main" and r["deleted_at"] is None]
    assert len(live_mains) == 1
    for image_id in (new_id, src):
        jobs = count(
            images_db.conn,
            "SELECT count(*) FROM job_runs WHERE entity_type = 'image' AND entity_id = %s",
            [image_id],
        )
        assert jobs >= 1
    row = candidate_row(images_db.conn, candidate)
    assert row["status"] == "accepted" and row["accepted_image_id"] == new_id
    actions = {
        r[0]
        for r in images_db.conn.execute(
            "SELECT action FROM audit_log WHERE entity_id IN (%s, %s, %s)", [candidate, src, new_id]
        ).fetchall()
    }
    assert {"image.demoted", "image.uploaded", "image_candidate.accepted"} <= actions


@pytest.mark.endpoint("image_candidates_accept", case="conflict")
async def test_accept_replay_and_conflicts(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    candidate, _, _ = await _ready(api, images_db, store)
    key = str(uuid.uuid4())
    first = await _decide(api, images_db, candidate, "accept", key=key)
    objects_after_first = len(store.objects)
    again = await _decide(api, images_db, candidate, "accept", key=key)
    assert first.status_code == again.status_code == 202 and first.json() == again.json()
    assert len(store.objects) == objects_after_first  # a replay copies no object
    other = await api.post(
        f"{CANDIDATES}/{uuid.uuid4()}/accept", headers=headers(images_db, key=key)
    )
    assert other.status_code == 409
    twice = await _decide(api, images_db, candidate, "accept")
    assert twice.status_code == 409
    assert twice.json()["error"]["type"] == "INVALID_STATUS_TRANSITION"


@pytest.mark.parametrize("status", ["rejected", "failed", "queued"])
async def test_accept_needs_ready(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage, status: str
) -> None:
    tenant = images_db.tenant("acme")
    p, src, _ = source_main(images_db.conn, store, tenant)
    cols: dict[str, Any] = {"status": status}
    if status == "rejected":
        cols["storage_key"] = "k"
    if status == "failed":
        cols["failure_reason"] = "fidelity"
    candidate = f.image_candidate(images_db.conn, tenant, p, src, **cols)
    before = len(store.objects)
    response = await _decide(api, images_db, candidate, "accept")
    assert response.status_code == 409
    assert len(store.objects) == before  # refused before any object copy


async def test_accept_after_a_new_main_or_a_deleted_source_is_409(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    from datetime import UTC, datetime

    candidate, _, src = await _ready(api, images_db, store)
    images_db.conn.execute(
        "UPDATE images SET clean_content_hash = %s WHERE id = %s", ["c" * 64, src]
    )
    assert (await _decide(api, images_db, candidate, "accept")).status_code == 409
    other, _, src2 = await _ready(api, images_db, store)
    images_db.conn.execute(
        "UPDATE images SET deleted_at = %s WHERE id = %s", [datetime.now(UTC), src2]
    )
    assert (await _decide(api, images_db, other, "accept")).status_code == 409


async def test_accept_a_size_chart_replaces_the_old_one(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    candidate, p, _ = await _ready(api, images_db, store, kind="size_chart")
    old = image_row(images_db.conn, images_db.tenant("acme"), p, slot="size_chart")
    response = await _decide(api, images_db, candidate, "accept")
    assert response.status_code == 202, response.text
    rows = {r["id"]: r for r in _images(images_db, p)}
    assert rows[old]["deleted_at"] is not None
    new = rows[uuid.UUID(response.json()["image_id"])]
    assert (new["slot"], new["origin"], new["parent_image_id"]) == ("size_chart", "rendered", None)


async def test_accept_races_a_main_upload_one_main(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    from datetime import UTC, datetime

    from app.services.image_keys import image_object_key

    from .images_support import picture, register_key

    candidate, p, _ = await _ready(api, images_db, store)
    tenant = images_db.tenant("acme")
    image_id = uuid.uuid4()
    key = image_object_key(tenant, p, image_id)
    store.objects[key] = (picture(1000, 1000), "image/jpeg", datetime.now(UTC))
    accept, upload = await asyncio.gather(
        _decide(api, images_db, candidate, "accept"),
        register_key(api, headers(images_db), p, key),
    )
    assert upload.status_code == 202
    mains = [r for r in _images(images_db, p) if r["slot"] == "main" and r["deleted_at"] is None]
    assert len(mains) == 1
    assert accept.status_code in (202, 409)


async def test_reject_then_create_again_is_ready_at_no_cost(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    candidate, p, src = await _ready(api, images_db, store)
    rejected = await _decide(api, images_db, candidate, "reject")
    assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"
    again = await _create(api, images_db, p, {"kind": "fix", "source_image_id": str(src)})
    assert again.json()["candidate_id"] == str(candidate) and again.json()["status"] == "ready"
    assert again.json()["job_id"] is None
    assert _jobs_for(images_db, candidate) == 1
    assert count(images_db.conn, "SELECT count(*) FROM agent_runs") == 0


@pytest.mark.endpoint("image_candidates_reject", case="success")
async def test_reject_has_no_previews_and_no_signed_url_is_stored(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    candidate, p, src = await _ready(api, images_db, store)
    rejected = await _decide(api, images_db, candidate, "reject")
    body = rejected.json()
    assert body["preview_url"] is None and body["source_preview_url"] is None
    assert candidate_row(images_db.conn, candidate)["storage_key"] is not None  # kept
    await _create(api, images_db, p, {"kind": "fix", "source_image_id": str(src)})
    other, _, _ = await _ready(api, images_db, store)
    await _decide(api, images_db, other, "accept")
    listed = await api.get(_url(p), headers=as_member(images_db, "operator@acme.test"))
    assert "X-Amz-Signature" in listed.text  # the list signs, and is not stored
    stored = images_db.conn.execute("SELECT response_json::text FROM idempotency_keys").fetchall()
    assert stored and not any(
        "X-Amz-Signature" in (b or "") or "X-Goog-Signature" in (b or "") for (b,) in stored
    )


@pytest.mark.endpoint("image_candidates_reject", case="conflict")
async def test_reject_twice_and_reject_conflicts(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    candidate, _, _ = await _ready(api, images_db, store)
    first = await _decide(api, images_db, candidate, "reject")
    second = await _decide(api, images_db, candidate, "reject")
    assert second.status_code == 200 and second.json() == first.json()
    tenant = images_db.tenant("acme")
    p, src, _ = source_main(images_db.conn, store, tenant)
    failed = f.image_candidate(
        images_db.conn, tenant, p, src, status="failed", failure_reason="fidelity"
    )
    assert (await _decide(api, images_db, failed, "reject")).status_code == 409
    key = str(uuid.uuid4())
    await _decide(api, images_db, candidate, "reject", key=key)
    reused = await api.post(f"{CANDIDATES}/{failed}/reject", headers=headers(images_db, key=key))
    assert reused.status_code == 409


async def test_reject_an_accepted_candidate_is_409(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    candidate, _, _ = await _ready(api, images_db, store)
    assert (await _decide(api, images_db, candidate, "accept")).status_code == 202
    assert (await _decide(api, images_db, candidate, "reject")).status_code == 409


@pytest.mark.endpoint("image_candidates_accept", case="wrong_role")
@pytest.mark.endpoint("image_candidates_reject", case="wrong_role")
@pytest.mark.parametrize("action", ["accept", "reject"])
async def test_reviewer_cannot_decide(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage, action: str
) -> None:
    candidate, _, _ = await _ready(api, images_db, store)
    response = await _decide(api, images_db, candidate, action, email="reviewer@acme.test")
    assert response.status_code == 403


@pytest.mark.endpoint("image_candidates_accept", case="wrong_tenant")
@pytest.mark.endpoint("image_candidates_reject", case="wrong_tenant")
@pytest.mark.parametrize("action", ["accept", "reject"])
async def test_other_tenants_candidate_is_404(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage, action: str
) -> None:
    candidate, _, _ = await _ready(api, images_db, store, slug="bharat")
    response = await _decide(api, images_db, candidate, action)
    assert response.status_code == 404
    assert candidate_row(images_db.conn, candidate)["status"] == "ready"


@pytest.mark.endpoint("image_candidates_accept", case="not_found")
@pytest.mark.endpoint("image_candidates_reject", case="not_found")
@pytest.mark.parametrize("action", ["accept", "reject"])
async def test_unknown_candidate_is_404(
    images_db: JobWorld, api: httpx.AsyncClient, action: str
) -> None:
    assert (await _decide(api, images_db, uuid.uuid4(), action)).status_code == 404


@pytest.mark.endpoint("image_candidates_accept", case="validation")
@pytest.mark.endpoint("image_candidates_reject", case="validation")
@pytest.mark.parametrize("action", ["accept", "reject"])
async def test_bad_candidate_id_is_422(
    images_db: JobWorld, api: httpx.AsyncClient, action: str
) -> None:
    assert (await _decide(api, images_db, "not-a-uuid", action)).status_code == 422


@pytest.mark.endpoint("image_candidates_accept", case="missing_auth")
@pytest.mark.endpoint("image_candidates_reject", case="missing_auth")
@pytest.mark.parametrize("action", ["accept", "reject"])
async def test_decide_missing_auth(
    images_db: JobWorld, api: httpx.AsyncClient, action: str
) -> None:
    response = await api.post(f"{CANDIDATES}/{uuid.uuid4()}/{action}")
    assert response.status_code == 401


@pytest.mark.endpoint("image_candidates_accept", case="invalid_token", variant="expired")
@pytest.mark.endpoint("image_candidates_accept", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("image_candidates_accept", case="invalid_token", variant="wrong_typ")
@pytest.mark.endpoint("image_candidates_reject", case="invalid_token", variant="expired")
@pytest.mark.endpoint("image_candidates_reject", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("image_candidates_reject", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
@pytest.mark.parametrize("action", ["accept", "reject"])
async def test_decide_invalid_token(
    images_db: JobWorld, api: httpx.AsyncClient, variant: str, action: str
) -> None:
    hdrs = {**bad_token(images_db, variant), "Idempotency-Key": str(uuid.uuid4())}
    response = await api.post(f"{CANDIDATES}/{uuid.uuid4()}/{action}", headers=hdrs)
    assert response.status_code == 401


async def test_object_copy_failure_writes_no_row(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    from app.adapters.storage import StorageUnavailable

    candidate, p, _ = await _ready(api, images_db, store)
    before = len(_images(images_db, p))
    store.fail = {"put": StorageUnavailable("down")}
    response = await _decide(api, images_db, candidate, "accept")
    assert response.status_code == 500
    assert len(_images(images_db, p)) == before
    assert candidate_row(images_db.conn, candidate)["status"] == "ready"


async def test_accept_caps(images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage) -> None:
    from .images_support import enqueue_image_job

    candidate, _, _ = await _ready(api, images_db, store)
    tenant = images_db.tenant("acme")
    for _ in range(19):  # 19 live + the 2 the accept adds > 20
        await enqueue_image_job(tenant, uuid.uuid4())
    assert (await _decide(api, images_db, candidate, "accept")).status_code == 429


async def test_deleting_the_source_fails_its_open_candidates(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    candidate, _, src = await _ready(api, images_db, store)
    key = candidate_row(images_db.conn, candidate)["storage_key"]
    assert key in store.objects
    response = await api.delete(
        f"/api/v1/images/{src}", headers=as_member(images_db, "operator@acme.test")
    )
    assert response.status_code == 204
    row = candidate_row(images_db.conn, candidate)
    assert (row["status"], row["failure_reason"]) == ("failed", "source_changed")
    assert key not in store.objects


# ---- review fixes: a candidate answers a create only for its own live source ----


@pytest.mark.parametrize("decided", ["ready", "rejected"])
async def test_same_photo_on_a_new_main_is_a_new_candidate(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage, decided: str
) -> None:
    from datetime import UTC, datetime

    candidate, p, src = await _ready(api, images_db, store)
    if decided == "rejected":
        assert (await _decide(api, images_db, candidate, "reject")).status_code == 200
    key = candidate_row(images_db.conn, candidate)["storage_key"]
    images_db.conn.execute(
        "UPDATE images SET deleted_at = %s WHERE id = %s", [datetime.now(UTC), src]
    )
    _, again_src, _ = source_main(images_db.conn, store, images_db.tenant("acme"), product_id=p)
    again = await _create(api, images_db, p, {"kind": "fix", "source_image_id": str(again_src)})
    assert again.status_code == 202, again.text
    assert again.json()["candidate_id"] != str(candidate)
    assert again.json()["status"] == "queued" and again.json()["job_id"] is not None
    old = candidate_row(images_db.conn, candidate)
    assert (old["status"], old["failure_reason"]) == ("failed", "source_changed")
    assert key not in store.objects
    await run_image_job(uuid.UUID(again.json()["job_id"]))
    fresh = uuid.UUID(again.json()["candidate_id"])
    assert (await _decide(api, images_db, fresh, "accept")).status_code == 202


async def test_a_main_upload_retires_the_old_mains_candidates(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    from datetime import UTC, datetime

    from app.services.image_keys import image_object_key

    from .images_support import picture, register_key

    candidate, p, _ = await _ready(api, images_db, store)
    key = candidate_row(images_db.conn, candidate)["storage_key"]
    image_id = uuid.uuid4()
    upload_key = image_object_key(images_db.tenant("acme"), p, image_id)
    store.objects[upload_key] = (picture(1000, 1000), "image/jpeg", datetime.now(UTC))
    upload = await register_key(api, headers(images_db), p, upload_key)
    assert upload.status_code == 202, upload.text
    row = candidate_row(images_db.conn, candidate)
    assert (row["status"], row["failure_reason"]) == ("failed", "source_changed")
    assert key not in store.objects


async def test_delete_then_reupload_of_the_same_photo_can_try_again(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    candidate, p, src = await _ready(api, images_db, store)
    deleted = await api.delete(
        f"/api/v1/images/{src}", headers=as_member(images_db, "operator@acme.test")
    )
    assert deleted.status_code == 204
    _, again_src, _ = source_main(images_db.conn, store, images_db.tenant("acme"), product_id=p)
    again = await _create(api, images_db, p, {"kind": "fix", "source_image_id": str(again_src)})
    assert again.status_code == 202, again.text
    assert again.json()["candidate_id"] != str(candidate)
    assert again.json()["status"] == "queued"


async def test_accept_a_size_chart_at_the_image_cap(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    from app.services.images import MAX_LIVE_IMAGES_PER_PRODUCT

    candidate, p, _ = await _ready(api, images_db, store, kind="size_chart")
    tenant = images_db.tenant("acme")
    image_row(images_db.conn, tenant, p, slot="size_chart")
    for _ in range(MAX_LIVE_IMAGES_PER_PRODUCT - 1):
        image_row(images_db.conn, tenant, p)
    response = await _decide(api, images_db, candidate, "accept")
    assert response.status_code == 202, response.text
    live = [r for r in _images(images_db, p) if r["deleted_at"] is None]
    assert len(live) == MAX_LIVE_IMAGES_PER_PRODUCT


# ---- SI10 D4 (joint test, SI10 merged after SI7): an AI edit passes the one admission rule ----


def _pause(jw: JobWorld, reason: str = "credit_exhausted") -> None:
    jw.conn.execute(
        "UPDATE ai_credit_state SET paused_reason = %s, paused_at = now() WHERE id = 1", [reason]
    )


@pytest.mark.parametrize(
    ("reason", "code"),
    [("credit_exhausted", "AI_CREDIT_EXHAUSTED"), ("key_invalid", "AI_PROVIDER_KEY_INVALID")],
)
async def test_white_background_while_ai_paused_is_503_and_writes_nothing(
    images_db: JobWorld,
    api: httpx.AsyncClient,
    store: FakeStorage,
    edit_on: pytest.MonkeyPatch,
    credit_reset: None,
    reason: str,
    code: str,
) -> None:
    tenant = images_db.tenant("acme")
    p, src, _ = source_main(images_db.conn, store, tenant)
    _pause(images_db, reason)
    tables = ("image_candidates", "job_runs", "idempotency_keys")
    before = [count(images_db.conn, f"SELECT count(*) FROM {t}") for t in tables]  # noqa: S608
    body = {"kind": "white_background", "source_image_id": str(src)}
    response = await _create(api, images_db, p, body)
    assert response.status_code == 503, response.text
    assert response.json()["error"]["type"] == code
    assert "retry-after" not in response.headers
    after = [count(images_db.conn, f"SELECT count(*) FROM {t}") for t in tables]  # noqa: S608
    assert after == before


async def test_fix_while_ai_paused_is_still_queued(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage, credit_reset: None
) -> None:
    """A zero-token kind spends no AI, so the pause never refuses it."""
    p, src, _ = source_main(images_db.conn, store, images_db.tenant("acme"))
    _pause(images_db)
    response = await _create(api, images_db, p, {"kind": "fix", "source_image_id": str(src)})
    assert response.status_code == 202 and response.json()["status"] == "queued"


async def test_ready_edit_still_answers_while_ai_paused(
    images_db: JobWorld,
    api: httpx.AsyncClient,
    store: FakeStorage,
    edit_on: pytest.MonkeyPatch,
    credit_reset: None,
) -> None:
    p, src, _ = source_main(images_db.conn, store, images_db.tenant("acme"))
    body = {"kind": "white_background", "source_image_id": str(src)}
    first = (await _create(api, images_db, p, body)).json()
    await run_image_job(uuid.UUID(first["job_id"]))
    _pause(images_db)
    again = await _create(api, images_db, p, body)
    assert again.status_code == 202, again.text
    assert (again.json()["candidate_id"], again.json()["status"]) == (
        first["candidate_id"],
        "ready",
    )


async def test_white_background_past_the_brand_cap_is_failed_budget_with_no_job(
    images_db: JobWorld,
    api: httpx.AsyncClient,
    store: FakeStorage,
    edit_on: pytest.MonkeyPatch,
    credit_reset: None,
) -> None:
    tenant = images_db.tenant("acme")
    p, src, _ = source_main(images_db.conn, store, tenant)
    images_db.conn.execute("UPDATE tenants SET llm_daily_budget_usd = 0.05 WHERE id = %s", [tenant])
    body = {"kind": "white_background", "source_image_id": str(src)}
    response = await _create(api, images_db, p, body)
    assert response.status_code == 202, response.text
    assert (response.json()["status"], response.json()["job_id"]) == ("failed", None)
    row = candidate_row(images_db.conn, uuid.UUID(response.json()["candidate_id"]))
    assert row["failure_reason"] == "budget"
    assert _jobs_for(images_db, response.json()["candidate_id"]) == 0


async def test_white_background_on_one_pooled_connection(
    images_db: JobWorld,
    api: httpx.AsyncClient,
    store: FakeStorage,
    edit_on: pytest.MonkeyPatch,
    credit_reset: None,
) -> None:
    """The admission read runs before the idempotent transaction, so one connection is enough
    (nested, it would wait out `pool_timeout` into a 500)."""
    from app.core.config import get_settings
    from app.db.session import dispose_engine

    p, src, _ = source_main(images_db.conn, store, images_db.tenant("acme"))
    edit_on.setenv("DATABASE_POOL_SIZE", "1")
    edit_on.setenv("DATABASE_MAX_OVERFLOW", "0")
    edit_on.setenv("DATABASE_POOL_TIMEOUT", "1")
    get_settings.cache_clear()
    await dispose_engine()
    body = {"kind": "white_background", "source_image_id": str(src)}
    response = await _create(api, images_db, p, body)
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "queued"


# ---- ADR-0030 amendment (2026-11-11): an accepted candidate answers only while applied ----


async def _accepted_main(
    api: httpx.AsyncClient, jw: JobWorld, store: FakeStorage, *, kind: str = "fix"
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """A candidate made and used through the API: (candidate, product, old source, new main)."""
    candidate, p, src = await _ready(api, jw, store, kind=kind)
    used = await _decide(api, jw, candidate, "accept")
    assert used.status_code == 202, used.text
    return candidate, p, src, uuid.UUID(used.json()["image_id"])


async def test_click_on_a_stale_page_answers_already_applied(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage, edit_on: pytest.MonkeyPatch
) -> None:
    """The page still shows the old main: the create answers the applied candidate and its
    image, with no new job and no AI cost."""
    candidate, p, src, new_main = await _accepted_main(
        api, images_db, store, kind="white_background"
    )
    jobs_before = count(images_db.conn, "SELECT count(*) FROM job_runs", [])
    again = await _create(
        api, images_db, p, {"kind": "white_background", "source_image_id": str(src)}
    )
    assert again.status_code == 202, again.text
    assert again.json() == {
        "candidate_id": str(candidate),
        "status": "accepted",
        "job_id": None,
        "poll_url": None,
        "accepted_image_id": str(new_main),
    }
    assert count(images_db.conn, "SELECT count(*) FROM job_runs", []) == jobs_before


async def test_another_kind_on_the_demoted_source_is_still_404(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage, edit_on: pytest.MonkeyPatch
) -> None:
    """Only a candidate of the asked kind made from this source answers for it."""
    _, p, src, _ = await _accepted_main(api, images_db, store, kind="fix")
    other = await _create(
        api, images_db, p, {"kind": "white_background", "source_image_id": str(src)}
    )
    assert other.status_code == 404
    assert other.json()["error"]["message"] == "source_image_id not found."


@pytest.mark.parametrize("case", ["deleted", "replaced"])
async def test_same_photo_after_the_applied_image_is_gone_is_a_new_job(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage, case: str
) -> None:
    """The applied image was deleted, or replaced by another main: the same pixels as a new
    main get a fresh candidate and job; a second click while it is queued is the same job."""
    from datetime import UTC, datetime

    candidate, p, src, new_main = await _accepted_main(api, images_db, store)
    if case == "deleted":
        images_db.conn.execute(
            "UPDATE images SET deleted_at = %s WHERE id = ANY(%s)",
            [datetime.now(UTC), [new_main, src]],
        )
    else:
        images_db.conn.execute("UPDATE images SET slot = 'secondary' WHERE id = %s", [new_main])
    stale = await _create(api, images_db, p, {"kind": "fix", "source_image_id": str(src)})
    assert stale.status_code == 404  # the old source no longer leads to a live main
    _, again_src, _ = source_main(images_db.conn, store, images_db.tenant("acme"), product_id=p)
    first = await _create(api, images_db, p, {"kind": "fix", "source_image_id": str(again_src)})
    assert first.status_code == 202, first.text
    made = first.json()
    assert made["candidate_id"] != str(candidate)
    assert made["status"] == "queued" and made["job_id"] is not None
    assert made["accepted_image_id"] is None
    second = await _create(api, images_db, p, {"kind": "fix", "source_image_id": str(again_src)})
    assert second.status_code == 202
    assert (second.json()["candidate_id"], second.json()["job_id"]) == (
        made["candidate_id"],
        made["job_id"],
    )
    assert _jobs_for(images_db, made["candidate_id"]) == 1
    assert candidate_row(images_db.conn, candidate)["status"] == "accepted"  # provenance kept


async def test_same_photo_as_another_colours_main_is_a_new_job(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    """The red Front was fixed and used; the same photo as the blue Front gets its own job."""
    tenant = images_db.tenant("acme")
    style = product(images_db.conn, tenant, "style")
    red = f.colourway(images_db.conn, tenant, style)
    blue = f.colourway(images_db.conn, tenant, style, position=2)
    _, red_src, digest = source_main(
        images_db.conn, store, tenant, product_id=style, colourway_id=red
    )
    made = await _create(api, images_db, style, {"kind": "fix", "source_image_id": str(red_src)})
    assert made.status_code == 202, made.text
    await run_image_job(uuid.UUID(made.json()["job_id"]))
    used = await _decide(api, images_db, made.json()["candidate_id"], "accept")
    assert used.status_code == 202, used.text
    _, blue_src, blue_digest = source_main(
        images_db.conn, store, tenant, product_id=style, colourway_id=blue
    )
    assert blue_digest == digest
    again = await _create(api, images_db, style, {"kind": "fix", "source_image_id": str(blue_src)})
    assert again.status_code == 202, again.text
    assert again.json()["candidate_id"] != made.json()["candidate_id"]
    assert again.json()["status"] == "queued" and again.json()["job_id"] is not None
    stale_red = await _create(
        api, images_db, style, {"kind": "fix", "source_image_id": str(red_src)}
    )
    assert stale_red.json()["status"] == "accepted"
    assert stale_red.json()["accepted_image_id"] == used.json()["image_id"]
