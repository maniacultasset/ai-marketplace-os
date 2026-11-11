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


# ---- SI10 T14 (AC12): the trial end ----


async def test_tenants_me_trial_ends_at(jobs_db: JobWorld, api: httpx.AsyncClient) -> None:
    acme = jobs_db.tenant("acme")
    jobs_db.conn.execute(
        "UPDATE tenants SET status = 'trial', trial_ends_at = '2026-11-21T00:00:00Z' WHERE id = %s",
        [acme],
    )
    body = (await api.get(URL, headers=as_member(jobs_db, "owner@acme.test"))).json()
    assert body["trial_ends_at"] == "2026-11-21T00:00:00Z"


async def test_tenants_me_active_tenant_hides_a_stale_trial_date(
    jobs_db: JobWorld, api: httpx.AsyncClient
) -> None:
    jobs_db.conn.execute(
        "UPDATE tenants SET status = 'active', trial_ends_at = '2026-10-01T00:00:00Z' "
        "WHERE id = %s",
        [jobs_db.tenant("acme")],
    )
    body = (await api.get(URL, headers=as_member(jobs_db, "owner@acme.test"))).json()
    assert body["trial_ends_at"] is None


CAPABILITY_ENV = {
    "amazon_publishing_enabled": "AMAZON_PUBLISH_ENABLED",
    "category_suggest_enabled": "CATEGORY_SUGGEST_ENABLED",
    "column_map_ai_enabled": "COLUMN_MAP_AI_ENABLED",
    "photo_qa_enabled": "PHOTO_QA_ENABLED",
    "category_lamps_enabled": "CATEGORY_LAMPS_ENABLED",
    "size_chart_read_enabled": "SIZE_CHART_READ_ENABLED",
    "image_edit_enabled": "IMAGE_EDIT_ENABLED",
    "tenant_create_enabled": "TENANT_CREATE_ENABLED",
}


def _set_switches(monkeypatch: pytest.MonkeyPatch, values: dict[str, bool]) -> None:
    from app.core.config import get_settings

    for key, on in values.items():
        monkeypatch.setenv(CAPABILITY_ENV[key], "true" if on else "false")
    get_settings.cache_clear()


@pytest.mark.parametrize("on", [True, False])
async def test_tenants_me_capabilities_follow_settings(
    jobs_db: JobWorld, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, on: bool
) -> None:
    from app.core.config import get_settings

    values = dict.fromkeys(CAPABILITY_ENV, on)
    try:
        _set_switches(monkeypatch, values)
        response = await api.get(URL, headers=as_member(jobs_db, "reviewer@acme.test"))
        assert response.status_code == 200, response.text
        assert response.json()["capabilities"] == values
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


async def test_tenants_me_publishing_paused_with_assists_on(
    jobs_db: JobWorld, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The prod shape: publishing off, the assists on; the tenant read says so (the connect
    screen reads it before any account row exists)."""
    from app.core.config import get_settings

    try:
        _set_switches(
            monkeypatch, {"amazon_publishing_enabled": False, "category_suggest_enabled": True}
        )
        body = (await api.get(URL, headers=as_member(jobs_db, "owner@acme.test"))).json()
        assert body["capabilities"]["amazon_publishing_enabled"] is False
        assert body["capabilities"]["category_suggest_enabled"] is True
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
