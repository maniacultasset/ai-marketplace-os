"""S16 T15 (AC0): `GET /api/v1/tenants/me`, as app_rw through the real app."""

from __future__ import annotations

import httpx
import pytest

from .auth_support import PREFIX
from .conftest import LOCAL_HOSTS
from .jobs_support import JobWorld, as_member, bad_token, jobs_db

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["jobs_db"]
URL = f"{PREFIX}/tenants/me"
ETAG = r'^"[0-9a-f-]{36}"$'


@pytest.mark.endpoint("tenants_me", case="success")
async def test_tenants_me_success(jobs_db: JobWorld, api: httpx.AsyncClient) -> None:
    jobs_db.conn.execute(
        "UPDATE tenants SET tone = 'Warm.', guidelines = '[\"g1\"]', glossary = '{\"a\": \"b\"}' WHERE id = %s",
        [jobs_db.tenant("acme")],
    )
    response = await api.get(URL, headers=as_member(jobs_db, "owner@acme.test"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == str(jobs_db.tenant("acme")) and body["slug"] == "acme"
    assert (body["tone"], body["guidelines"], body["glossary"]) == ("Warm.", ["g1"], {"a": "b"})
    import re

    assert re.match(ETAG, response.headers["ETag"])


@pytest.mark.parametrize("role", ["owner", "operator", "reviewer", "admin"])
async def test_tenants_me_role_each_ok(
    jobs_db: JobWorld, api: httpx.AsyncClient, role: str
) -> None:
    response = await api.get(URL, headers=as_member(jobs_db, f"{role}@acme.test"))
    assert response.status_code == 200
    assert (response.json()["guidelines"], response.json()["glossary"]) == ([], {})


@pytest.mark.endpoint("tenants_me", case="wrong_tenant")
async def test_tenants_me_wrong_tenant_reads_own(jobs_db: JobWorld, api: httpx.AsyncClient) -> None:
    response = await api.get(URL, headers=as_member(jobs_db, "owner@bharat.test"))
    assert response.status_code == 200 and response.json()["slug"] == "bharat"


async def test_tenants_me_tid_without_membership_401(
    jobs_db: JobWorld, api: httpx.AsyncClient
) -> None:
    from .auth_support import bearer, tokens_for

    token = tokens_for(jobs_db.world, "owner@acme.test", "bharat", role="owner")
    response = await api.get(URL, headers=bearer(token))
    assert response.status_code == 401


async def test_tenants_me_deactivated_401(jobs_db: JobWorld, api: httpx.AsyncClient) -> None:
    jobs_db.conn.execute(
        "UPDATE tenant_memberships SET is_active = false, deactivated_at = now() WHERE user_id = %s",
        [jobs_db.world.user("reviewer@acme.test")],
    )
    response = await api.get(URL, headers=as_member(jobs_db, "reviewer@acme.test"))
    assert response.status_code == 401


@pytest.mark.endpoint("tenants_me", case="missing_auth")
async def test_tenants_me_missing_auth_401(jobs_db: JobWorld, api: httpx.AsyncClient) -> None:
    assert (await api.get(URL)).status_code == 401


@pytest.mark.endpoint("tenants_me", case="invalid_token", variant="expired")
@pytest.mark.endpoint("tenants_me", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("tenants_me", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_tenants_me_invalid_token_401(
    jobs_db: JobWorld, api: httpx.AsyncClient, variant: str
) -> None:
    response = await api.get(URL, headers=bad_token(jobs_db, variant))
    assert response.status_code == 401 and response.json()["error"]["type"] == "UNAUTHENTICATED"
