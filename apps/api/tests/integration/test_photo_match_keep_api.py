"""V1.1 (client feedback item 9): `POST /api/v1/images/{image_id}/photo-match/keep` as app_rw. The
seller keeps a photo the photo match questioned; who and when join the stored match, once, with
one audit row, and the approval gate stops judging it."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
from psycopg.types.json import Jsonb

from .conftest import LOCAL_HOSTS
from .images_support import image_db_row, image_row, images_db, product
from .jobs_support import JobWorld, as_member, bad_token, count

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["images_db"]
PREFIX = "/api/v1/images"
HASH = "c" * 64


def _match(status: str = "mismatch", **extra: Any) -> dict[str, Any]:
    return {
        "status": status,
        "key": "k" * 64,
        "source_hash": HASH,
        "colour": "Green",
        "job_id": str(uuid.uuid4()),
        "queued_at": "2026-11-11T10:00:00+00:00",
        "checked_at": "2026-11-11T10:00:05+00:00",
        "product_match": 0.93,
        "colour_match": 0.05,
        "seen_colour": "red",
        "reason": "Same design, but the photo shows another colour.",
        "question": "Is this photo the Green Kurta?" if status in ("mismatch", "unsure") else None,
        **extra,
    }


def _photo(jw: JobWorld, slug: str = "acme", match: dict[str, Any] | None = None) -> uuid.UUID:
    tenant = jw.tenant(slug)
    p = product(jw.conn, tenant)
    image_id = image_row(jw.conn, tenant, p, slot="secondary")
    metadata = {"vision": {"w": 64, "h": 64}}
    if match is not None:
        metadata["photo_match"] = match
    jw.conn.execute(
        "UPDATE images SET clean_content_hash = %s, clean_mime_type = 'image/jpeg', "
        "compliance_status = 'passed', metadata = %s WHERE id = %s",
        [HASH, Jsonb(metadata), image_id],
    )
    return image_id


def _keep(
    api: httpx.AsyncClient,
    jw: JobWorld,
    image_id: Any,
    *,
    email: str = "operator@acme.test",
    key: str | None = None,
) -> Any:
    headers = {**as_member(jw, email), "Idempotency-Key": key or str(uuid.uuid4())}
    return api.post(f"{PREFIX}/{image_id}/photo-match/keep", headers=headers)


def _stored(jw: JobWorld, image_id: uuid.UUID) -> dict[str, Any]:
    stored = image_db_row(jw.conn, image_id)["metadata"]["photo_match"]
    return stored if isinstance(stored, dict) else json.loads(stored)


def _kept_audits(jw: JobWorld, image_id: uuid.UUID) -> int:
    return count(
        jw.conn,
        "SELECT count(*) FROM audit_log WHERE action = 'image.photo_match_kept' AND entity_id = %s",
        [image_id],
    )


@pytest.mark.endpoint("images_photo_match_keep", case="success")
async def test_keep_records_who_and_when_once(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    image_id = _photo(images_db, match=_match())
    response = await _keep(api, images_db, image_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["image_id"] == str(image_id)
    match = body["photo_match"]
    assert match["status"] == "mismatch" and match["kept"] is True
    assert match["kept_at"] is not None and match["kept_by_user_id"] is not None
    stored = _stored(images_db, image_id)
    assert stored["kept_by"] == match["kept_by_user_id"] and stored["kept_at"]
    assert stored["status"] == "mismatch" and stored["key"] == "k" * 64  # the verdict stays
    assert _kept_audits(images_db, image_id) == 1
    # a second keep (a new key) answers the first keep's record and writes nothing new
    again = await _keep(api, images_db, image_id)
    assert again.status_code == 200
    assert again.json()["photo_match"]["kept_at"] == match["kept_at"]
    assert _kept_audits(images_db, image_id) == 1


async def test_an_unsure_photo_can_be_kept(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    image_id = _photo(images_db, match=_match("unsure", colour_match=0.5))
    response = await _keep(api, images_db, image_id)
    assert response.status_code == 200, response.text
    assert response.json()["photo_match"]["kept"] is True


@pytest.mark.endpoint("images_photo_match_keep", case="conflict")
async def test_no_question_on_the_photo_is_409(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    for match in (None, _match("match"), _match("checking"), _match("failed")):
        image_id = _photo(images_db, match=match)
        response = await _keep(api, images_db, image_id)
        assert response.status_code == 409, (match, response.text)
        assert response.json()["error"]["type"] == "INVALID_STATUS_TRANSITION"
    # a match made for other pixels is no question either
    stale = _photo(images_db, match=_match(source_hash="d" * 64))
    assert (await _keep(api, images_db, stale)).status_code == 409
    # the same key with another image is a reused key
    first = _photo(images_db, match=_match())
    second = _photo(images_db, match=_match())
    key = str(uuid.uuid4())
    assert (await _keep(api, images_db, first, key=key)).status_code == 200
    reused = await _keep(api, images_db, second, key=key)
    assert reused.status_code in (409, 422)


@pytest.mark.endpoint("images_photo_match_keep", case="validation")
async def test_bad_id_422(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    assert (await _keep(api, images_db, "not-a-uuid")).status_code == 422


@pytest.mark.endpoint("images_photo_match_keep", case="not_found")
async def test_unknown_image_404(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    assert (await _keep(api, images_db, uuid.uuid4())).status_code == 404


@pytest.mark.endpoint("images_photo_match_keep", case="wrong_tenant")
async def test_other_tenant_image_404(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    image_id = _photo(images_db, "bharat", match=_match())
    assert (await _keep(api, images_db, image_id)).status_code == 404
    assert "kept_at" not in _stored(images_db, image_id)


@pytest.mark.endpoint("images_photo_match_keep", case="wrong_role")
async def test_reviewer_403(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    image_id = _photo(images_db, match=_match())
    response = await _keep(api, images_db, image_id, email="reviewer@acme.test")
    assert response.status_code == 403
    assert "kept_at" not in _stored(images_db, image_id)


@pytest.mark.endpoint("images_photo_match_keep", case="missing_auth")
async def test_missing_auth_401(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    response = await api.post(
        f"{PREFIX}/{uuid.uuid4()}/photo-match/keep", headers={"Idempotency-Key": str(uuid.uuid4())}
    )
    assert response.status_code == 401


@pytest.mark.endpoint("images_photo_match_keep", case="invalid_token", variant="expired")
@pytest.mark.endpoint("images_photo_match_keep", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("images_photo_match_keep", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_invalid_token_401(images_db: JobWorld, api: httpx.AsyncClient, variant: str) -> None:
    headers = {**bad_token(images_db, variant), "Idempotency-Key": str(uuid.uuid4())}
    response = await api.post(f"{PREFIX}/{uuid.uuid4()}/photo-match/keep", headers=headers)
    assert response.status_code == 401
