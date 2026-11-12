"""Client feedback item 7, bullet 3: `PATCH /api/v1/images/{image_id}` as app_rw. The seller moves
a saved photo to another view without uploading it again; a taken Front, Back or Close-up swaps,
a changed slot is checked again, and each photo moved has one `image.view_changed` audit row."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
from psycopg.types.json import Jsonb

from . import factories as f
from .conftest import LOCAL_HOSTS
from .images_support import image_db_row, image_row, images_db, product, style_and_child
from .jobs_support import JobWorld, as_member, bad_token, count

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["images_db"]
PREFIX = "/api/v1/images"
REPORT = {"amazon": {"marketplace": "amazon", "slot": "secondary", "pass": True, "checks": []}}


def _photo(
    jw: JobWorld, product_id: uuid.UUID, slot: str, role: str | None = None, **cols: Any
) -> uuid.UUID:
    tenant = jw.conn.execute(
        "SELECT tenant_id FROM products WHERE id = %s", [product_id]
    ).fetchone()
    assert tenant is not None
    return image_row(
        jw.conn, tenant[0], product_id, slot=slot, role=role, compliance_status="passed", **cols
    )


def _move(
    api: httpx.AsyncClient,
    jw: JobWorld,
    image_id: Any,
    body: dict[str, Any],
    *,
    email: str = "operator@acme.test",
) -> Any:
    return api.patch(f"{PREFIX}/{image_id}", json=body, headers=as_member(jw, email))


def _view(jw: JobWorld, image_id: uuid.UUID) -> tuple[str, str | None]:
    row = image_db_row(jw.conn, image_id)
    return row["slot"], row["role"]


def _audits(jw: JobWorld, image_id: uuid.UUID) -> list[tuple[Any, Any, str]]:
    rows = jw.conn.execute(
        "SELECT before, after, reason FROM audit_log WHERE action = 'image.view_changed' "
        "AND entity_id = %s ORDER BY occurred_at, id",
        [image_id],
    ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def _checks(jw: JobWorld, image_id: uuid.UUID) -> int:
    return count(
        jw.conn,
        "SELECT count(*) FROM job_runs WHERE kind = 'image_compliance' AND entity_type = 'image' "
        "AND entity_id = %s",
        [image_id],
    )


@pytest.mark.endpoint("images_update", case="success")
async def test_an_extra_photo_becomes_the_back(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    p = product(images_db.conn, images_db.tenant("acme"))
    extra = _photo(images_db, p, "secondary", image_report_json=Jsonb(REPORT))
    response = await _move(api, images_db, extra, {"slot": "secondary", "role": "back"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["image_id"] == str(extra)
    assert (body["slot"], body["role"]) == ("secondary", "back")
    # the same slot keeps its checks: no new job, the report stays
    assert body["compliance_status"] == "passed" and body["job_id"] is None
    assert body["swapped"] is None
    assert _view(images_db, extra) == ("secondary", "back")
    assert image_db_row(images_db.conn, extra)["image_report_json"] is not None
    assert _checks(images_db, extra) == 0
    [(before, after, reason)] = _audits(images_db, extra)
    assert reason == "extra->back"
    assert before == {"slot": "secondary"} and after == {"slot": "secondary"}


async def test_the_same_view_again_changes_nothing(
    images_db: JobWorld, api: httpx.AsyncClient
) -> None:
    p = product(images_db.conn, images_db.tenant("acme"))
    back = _photo(images_db, p, "secondary", "back")
    response = await _move(api, images_db, back, {"slot": "secondary", "role": "back"})
    assert response.status_code == 200, response.text
    assert response.json()["job_id"] is None and response.json()["swapped"] is None
    assert _audits(images_db, back) == []


async def test_a_taken_back_swaps_with_the_close_up(
    images_db: JobWorld, api: httpx.AsyncClient
) -> None:
    p = product(images_db.conn, images_db.tenant("acme"))
    back = _photo(images_db, p, "secondary", "back")
    close = _photo(images_db, p, "secondary", "close_up")
    response = await _move(api, images_db, close, {"slot": "secondary", "role": "back"})
    assert response.status_code == 200, response.text
    swapped = response.json()["swapped"]
    assert swapped["image_id"] == str(back) and swapped["role"] == "close_up"
    assert _view(images_db, close) == ("secondary", "back")
    assert _view(images_db, back) == ("secondary", "close_up")
    assert [a[2] for a in _audits(images_db, close)] == ["close_up->back"]
    assert [a[2] for a in _audits(images_db, back)] == ["back->close_up"]


async def test_a_back_moved_to_front_swaps_and_both_are_checked_again(
    images_db: JobWorld, api: httpx.AsyncClient
) -> None:
    tenant = images_db.tenant("acme")
    p = product(images_db.conn, tenant)
    front = _photo(images_db, p, "main", image_report_json=Jsonb(REPORT))
    back = _photo(images_db, p, "secondary", "back", image_report_json=Jsonb(REPORT))
    candidate = f.image_candidate(images_db.conn, tenant, p, front)
    response = await _move(api, images_db, back, {"slot": "main"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["slot"], body["role"], body["compliance_status"]) == ("main", None, "pending")
    assert body["job_id"] is not None
    swapped = body["swapped"]
    assert swapped["image_id"] == str(front)
    assert (swapped["slot"], swapped["role"], swapped["compliance_status"]) == (
        "secondary",
        "back",
        "pending",
    )
    assert swapped["job_id"] is not None
    for image_id in (front, back):
        row = image_db_row(images_db.conn, image_id)
        assert row["compliance_status"] == "pending" and row["image_report_json"] is None
        assert _checks(images_db, image_id) == 1
    assert _view(images_db, back) == ("main", None)
    assert image_db_row(images_db.conn, back)["position"] == 0
    assert _view(images_db, front) == ("secondary", "back")
    # the old Front's open white-background candidate can no longer be accepted
    status = images_db.conn.execute(
        "SELECT status FROM image_candidates WHERE id = %s", [candidate]
    ).fetchone()
    assert status is not None and status[0] == "failed"
    [(before, after, reason)] = _audits(images_db, back)
    assert (before, after, reason) == ({"slot": "secondary"}, {"slot": "main"}, "back->front")
    assert [a[2] for a in _audits(images_db, front)] == ["front->back"]


async def test_a_move_into_a_free_front_swaps_nothing(
    images_db: JobWorld, api: httpx.AsyncClient
) -> None:
    p = product(images_db.conn, images_db.tenant("acme"))
    lifestyle = _photo(images_db, p, "lifestyle")
    response = await _move(api, images_db, lifestyle, {"slot": "main"})
    assert response.status_code == 200, response.text
    assert response.json()["swapped"] is None
    assert _view(images_db, lifestyle) == ("main", None)


async def test_each_colour_has_its_own_front(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    tenant = images_db.tenant("acme")
    style = product(images_db.conn, tenant, "style")
    green = f.colourway(images_db.conn, tenant, style)
    shared_front = _photo(images_db, style, "main")
    green_extra = _photo(images_db, style, "secondary", colourway_id=green)
    response = await _move(api, images_db, green_extra, {"slot": "main"})
    assert response.status_code == 200, response.text
    assert response.json()["swapped"] is None  # the shared Front is another colour's
    assert _view(images_db, shared_front) == ("main", None)


async def test_a_photo_moved_off_the_match_drops_its_question(
    images_db: JobWorld, api: httpx.AsyncClient
) -> None:
    p = product(images_db.conn, images_db.tenant("acme"))
    match = {"status": "mismatch", "key": "k" * 64, "source_hash": "c" * 64}
    photo = _photo(images_db, p, "secondary", metadata=Jsonb({"photo_match": match}))
    response = await _move(api, images_db, photo, {"slot": "packaging_label"})
    assert response.status_code == 200, response.text
    metadata = image_db_row(images_db.conn, photo)["metadata"]
    stored = metadata if isinstance(metadata, dict) else json.loads(metadata)
    assert "photo_match" not in stored


@pytest.mark.endpoint("images_update", case="validation")
async def test_bad_bodies_422(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    tenant = images_db.tenant("acme")
    p = product(images_db.conn, tenant)
    photo = _photo(images_db, p, "secondary")
    for body in (
        {"slot": "main", "role": "back"},  # a role only with secondary
        {"slot": "hero"},
        {"role": "back"},
        {"slot": "secondary", "role": "side"},
    ):
        response = await _move(api, images_db, photo, body)
        assert response.status_code == 422, (body, response.text)
    assert (await _move(api, images_db, "not-a-uuid", {"slot": "main"})).status_code == 422
    # a size chart belongs to a style or a standalone, never a variant
    _style, child = style_and_child(images_db.conn, tenant)
    on_child = _photo(images_db, child, "secondary")
    response = await _move(api, images_db, on_child, {"slot": "size_chart"})
    assert response.status_code == 422, response.text
    assert _view(images_db, on_child) == ("secondary", None)
    assert _audits(images_db, photo) == [] and _audits(images_db, on_child) == []


@pytest.mark.endpoint("images_update", case="not_found")
async def test_unknown_or_deleted_image_404(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    assert (await _move(api, images_db, uuid.uuid4(), {"slot": "main"})).status_code == 404
    p = product(images_db.conn, images_db.tenant("acme"))
    gone = _photo(images_db, p, "secondary")
    images_db.conn.execute("UPDATE images SET deleted_at = now() WHERE id = %s", [gone])
    assert (await _move(api, images_db, gone, {"slot": "main"})).status_code == 404


@pytest.mark.endpoint("images_update", case="wrong_tenant")
async def test_other_tenant_image_404(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    p = product(images_db.conn, images_db.tenant("bharat"))
    photo = _photo(images_db, p, "secondary")
    assert (await _move(api, images_db, photo, {"slot": "main"})).status_code == 404
    assert _view(images_db, photo) == ("secondary", None)


@pytest.mark.endpoint("images_update", case="wrong_role")
async def test_reviewer_403(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    p = product(images_db.conn, images_db.tenant("acme"))
    photo = _photo(images_db, p, "secondary")
    response = await _move(api, images_db, photo, {"slot": "main"}, email="reviewer@acme.test")
    assert response.status_code == 403
    assert _view(images_db, photo) == ("secondary", None)


@pytest.mark.endpoint("images_update", case="missing_auth")
async def test_missing_auth_401(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    response = await api.patch(f"{PREFIX}/{uuid.uuid4()}", json={"slot": "main"})
    assert response.status_code == 401


@pytest.mark.endpoint("images_update", case="invalid_token", variant="expired")
@pytest.mark.endpoint("images_update", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("images_update", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_invalid_token_401(images_db: JobWorld, api: httpx.AsyncClient, variant: str) -> None:
    response = await api.patch(
        f"{PREFIX}/{uuid.uuid4()}", json={"slot": "main"}, headers=bad_token(images_db, variant)
    )
    assert response.status_code == 401
