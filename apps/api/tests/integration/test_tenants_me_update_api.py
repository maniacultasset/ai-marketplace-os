"""S16 T16 (AC0, AC5): `PATCH /api/v1/tenants/me` under `If-Match`, as app_rw through the real
app."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from .auth_support import PREFIX
from .conftest import LOCAL_HOSTS
from .jobs_support import JobWorld, as_member, bad_token, count, jobs_db

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["jobs_db"]
URL = f"{PREFIX}/tenants/me"


async def _etag(api: httpx.AsyncClient, jw: JobWorld, email: str = "owner@acme.test") -> str:
    response = await api.get(URL, headers=as_member(jw, email))
    return str(response.headers["ETag"])


async def _patch(
    api: httpx.AsyncClient,
    jw: JobWorld,
    body: object,
    *,
    email: str = "owner@acme.test",
    etag: str | None = None,
    **headers: str,
) -> httpx.Response:
    tag = etag if etag is not None else await _etag(api, jw, email)
    return await api.patch(
        URL, json=body, headers={**as_member(jw, email), "If-Match": tag, **headers}
    )


def _profile_jobs(jw: JobWorld) -> int:
    return count(
        jw.conn, "SELECT count(*) FROM job_runs WHERE payload_json->>'source' = 'brand_profile'"
    )


@pytest.mark.endpoint("tenants_me_update", case="success")
@pytest.mark.parametrize("role", ["owner", "admin"])
async def test_tenants_me_update_success(
    jobs_db: JobWorld, api: httpx.AsyncClient, role: str
) -> None:
    response = await _patch(
        api,
        jobs_db,
        {"name": "Acme Co", "guidelines": ["Name the fabric."]},
        email=f"{role}@acme.test",
    )
    assert response.status_code == 200, response.text
    assert (response.json()["name"], response.json()["guidelines"]) == (
        "Acme Co",
        ["Name the fabric."],
    )
    assert response.headers["ETag"] != ""


async def test_tenants_me_update_then_get_same_etag(
    jobs_db: JobWorld, api: httpx.AsyncClient
) -> None:
    response = await _patch(api, jobs_db, {"tone": "Warm."})
    assert response.headers["ETag"] == await _etag(api, jobs_db)


async def test_tenants_me_update_profile_job_enqueued(
    jobs_db: JobWorld, api: httpx.AsyncClient
) -> None:
    await _patch(api, jobs_db, {"glossary": {"kurta outfit": "kurta set"}})
    row = jobs_db.conn.execute(
        "SELECT kind, entity_type, entity_id, created_by FROM job_runs"
    ).fetchone()
    assert row == ("brand_embed", "brand_document", jobs_db.tenant("acme"), jobs_db.owner("acme"))


async def test_tenants_me_update_name_only_no_job(
    jobs_db: JobWorld, api: httpx.AsyncClient
) -> None:
    await _patch(api, jobs_db, {"name": "Renamed"})
    assert _profile_jobs(jobs_db) == 0


async def test_tenants_me_update_coalesces(jobs_db: JobWorld, api: httpx.AsyncClient) -> None:
    for i in range(4):
        assert (await _patch(api, jobs_db, {"tone": f"Tone {i}."})).status_code == 200
    assert _profile_jobs(jobs_db) == 1


async def test_tenants_me_update_audit_row(jobs_db: JobWorld, api: httpx.AsyncClient) -> None:
    await _patch(
        api, jobs_db, {"name": "Acme", "tone": "Warm."}, **{"X-Request-ID": "req-patch-16"}
    )
    rows = jobs_db.conn.execute(
        "SELECT entity_type, before, after, request_id FROM audit_log WHERE action = 'tenant.profile_updated'"
    ).fetchall()
    assert len(rows) == 1
    entity_type, before, after, request_id = rows[0]
    assert entity_type == "tenant" and request_id == "req-patch-16"
    assert after == {"tone": "Warm."} and before == {"tone": None}
    assert "@" not in json.dumps(after)


async def test_tenants_me_update_fault_order(jobs_db: JobWorld, api: httpx.AsyncClient) -> None:
    no_token = await api.patch(URL, json={"bogus": 1}, headers={"If-Match": '"' + "0" * 36 + '"'})
    assert no_token.status_code == 401
    tag = await _etag(api, jobs_db, "operator@acme.test")
    operator = await api.patch(
        URL,
        json={"bogus": 1},
        headers={**as_member(jobs_db, "operator@acme.test"), "If-Match": tag},
    )
    assert operator.status_code == 403


@pytest.mark.endpoint("tenants_me_update", case="validation")
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"tenant_id": "x", "name": "a"},
        {"status": "active"},
        {"name": ""},
        {"name": None},
        {"tone": "a\x00b"},
        {"guidelines": ["x" * 501]},
    ],
)
async def test_tenants_me_update_validation_422(
    jobs_db: JobWorld, api: httpx.AsyncClient, body: object
) -> None:
    response = await _patch(api, jobs_db, body)
    assert response.status_code == 422 and response.json()["error"]["type"] == "VALIDATION_FAILED"
    assert _profile_jobs(jobs_db) == 0


@pytest.mark.parametrize("tag", ["", "not-quoted", 'W/"' + "0" * 36 + '"'])
async def test_tenants_me_update_bad_if_match_422(
    jobs_db: JobWorld, api: httpx.AsyncClient, tag: str
) -> None:
    response = await _patch(api, jobs_db, {"tone": "x"}, etag=tag)
    assert response.status_code == 422


async def test_tenants_me_update_missing_if_match_422(
    jobs_db: JobWorld, api: httpx.AsyncClient
) -> None:
    response = await api.patch(
        URL, json={"tone": "x"}, headers=as_member(jobs_db, "owner@acme.test")
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "guideline",
    ['{"api_key": "x"}', "Atzr|" + "IwEBIA1b2c3d4e5f6g7h8i9j0", "AKIA" + "ABCDEFGHIJKLMNOP"],
)
async def test_tenants_me_update_secret_guideline_422(
    jobs_db: JobWorld, api: httpx.AsyncClient, guideline: str
) -> None:
    response = await _patch(api, jobs_db, {"guidelines": ["fine", guideline]})
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["loc"] == ["body", "guidelines", 1]
    assert guideline not in response.text
    assert _profile_jobs(jobs_db) == 0


@pytest.mark.endpoint("tenants_me_update", case="wrong_role")
@pytest.mark.parametrize("role", ["operator", "reviewer"])
async def test_tenants_me_update_wrong_role_403(
    jobs_db: JobWorld, api: httpx.AsyncClient, role: str
) -> None:
    response = await _patch(api, jobs_db, {"tone": "x"}, email=f"{role}@acme.test")
    assert response.status_code == 403


@pytest.mark.endpoint("tenants_me_update", case="wrong_tenant")
async def test_tenants_me_update_bharat_etag_on_acme_409(
    jobs_db: JobWorld, api: httpx.AsyncClient
) -> None:
    theirs = await _etag(api, jobs_db, "owner@bharat.test")
    response = await _patch(api, jobs_db, {"name": "Hijack"}, etag=theirs)
    assert response.status_code == 409
    row = jobs_db.conn.execute(
        "SELECT name FROM tenants WHERE id = %s", [jobs_db.tenant("bharat")]
    ).fetchone()
    assert row is not None and row[0] != "Hijack"


@pytest.mark.endpoint("tenants_me_update", case="conflict")
async def test_tenants_me_update_stale_etag_409_no_change(
    jobs_db: JobWorld, api: httpx.AsyncClient
) -> None:
    old = await _etag(api, jobs_db)
    assert (await _patch(api, jobs_db, {"tone": "First."}, etag=old)).status_code == 200
    response = await _patch(api, jobs_db, {"tone": "Second."}, etag=old)
    assert response.status_code == 409 and response.json()["error"]["type"] == "STALE_VERSION"
    assert (await api.get(URL, headers=as_member(jobs_db, "owner@acme.test"))).json()[
        "tone"
    ] == "First."


async def test_tenants_me_update_concurrent_same_etag_one_wins(
    jobs_db: JobWorld, api: httpx.AsyncClient
) -> None:
    tag = await _etag(api, jobs_db)
    results = await asyncio.gather(
        *(_patch(api, jobs_db, {"tone": f"T{i}."}, etag=tag) for i in range(4))
    )
    assert sorted(r.status_code for r in results) == [200, 409, 409, 409]


@pytest.mark.endpoint("tenants_me_update", case="missing_auth")
async def test_tenants_me_update_missing_auth_401(
    jobs_db: JobWorld, api: httpx.AsyncClient
) -> None:
    response = await api.patch(URL, json={"tone": "x"}, headers={"If-Match": '"' + "0" * 36 + '"'})
    assert response.status_code == 401


@pytest.mark.endpoint("tenants_me_update", case="invalid_token", variant="expired")
@pytest.mark.endpoint("tenants_me_update", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("tenants_me_update", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_tenants_me_update_invalid_token_401(
    jobs_db: JobWorld, api: httpx.AsyncClient, variant: str
) -> None:
    response = await api.patch(
        URL,
        json={"tone": "x"},
        headers={**bad_token(jobs_db, variant), "If-Match": '"' + "0" * 36 + '"'},
    )
    assert response.status_code == 401
