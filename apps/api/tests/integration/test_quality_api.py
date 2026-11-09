"""SI14 T20 (AC10): `GET /analytics/quality` (the caller's tenant) and `GET /admin/quality` (the
platform admin, every tenant or one) over HTTP."""

from __future__ import annotations

import types
import uuid
from typing import Any

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world
from .jobs_support import bad_token
from .quality_support import scenario

__all__ = ["gen_world"]
pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
QUALITY = "/api/v1/analytics/quality"
ADMIN_QUALITY = "/api/v1/admin/quality"
ADMIN = "operator@acme.test"


def _hdrs(world: GenWorld, email: str) -> dict[str, str]:
    out = world.headers(email)
    out.pop("Idempotency-Key", None)
    return out


def _admin(world: GenWorld) -> None:
    world.conn.execute("UPDATE users SET is_platform_admin = true WHERE email = %s", [ADMIN])
    world.conn.commit()


def _bad(world: GenWorld, variant: str) -> dict[str, str]:
    shim = types.SimpleNamespace(world=world, tenant=world.tenant)
    return bad_token(shim, variant)  # type: ignore[arg-type]


@pytest.mark.endpoint("analytics_quality", case="success")
async def test_quality_numbers(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    scenario(gen_world, "bharat")
    response = await api.get(QUALITY, headers=_hdrs(gen_world, "reviewer@bharat.test"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["listings_generated"] == 2
    assert body["first_pass_rate"] == 0.5 and body["edit_rate"] == 0.5
    assert body["blocked_rate"] == 0.5
    assert body["cost_per_listing_usd"] == 0.01
    markets = {m["marketplace"]: m for m in body["by_marketplace"]}
    assert markets["amazon"]["first_pass_rate"] == 1.0 and markets["flipkart"]["edit_rate"] == 1.0
    assert body["edited_fields"] == [
        {"path": "bullets.0", "count": 1},
        {"path": "title", "count": 1},
    ]
    assert body["by_tenant"] is None


async def test_an_empty_tenant_has_null_rates(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    response = await api.get(
        QUALITY,
        headers=_hdrs(gen_world, "owner@bharat.test"),
        params={"from": "2020-01-01", "to": "2020-01-31"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["listings_generated"] == 0
    assert body["first_pass_rate"] is None and body["cost_per_listing_usd"] is None


@pytest.mark.endpoint("analytics_quality", case="validation")
async def test_quality_bad_range(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    hdrs = _hdrs(gen_world, "owner@acme.test")
    assert (
        await api.get(QUALITY, headers=hdrs, params={"from": "2026-11-09", "to": "2026-11-01"})
    ).status_code == 422
    assert (await api.get(QUALITY, headers=hdrs, params={"from": "yesterday"})).status_code == 422


async def test_quality_busy_429(
    api: httpx.AsyncClient, gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.repositories import rate_limits

    async def full(*_: Any) -> None:
        return None

    monkeypatch.setattr(rate_limits, "inflight_acquire", full)
    response = await api.get(QUALITY, headers=_hdrs(gen_world, "owner@acme.test"))
    assert response.status_code == 429 and "Retry-After" in response.headers


@pytest.mark.endpoint("analytics_quality", case="wrong_tenant")
async def test_quality_is_the_callers_tenant(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    scenario(gen_world, "bharat")
    theirs = (await api.get(QUALITY, headers=_hdrs(gen_world, "owner@bharat.test"))).json()
    mine = (await api.get(QUALITY, headers=_hdrs(gen_world, "owner@acme.test"))).json()
    assert theirs["edited_fields"] and mine["edited_fields"] != theirs["edited_fields"]


@pytest.mark.endpoint("analytics_quality", case="missing_auth")
async def test_quality_missing_auth(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    assert (await api.get(QUALITY)).status_code == 401


@pytest.mark.endpoint("analytics_quality", case="invalid_token", variant="expired")
@pytest.mark.endpoint("analytics_quality", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("analytics_quality", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_quality_invalid_token(
    api: httpx.AsyncClient, gen_world: GenWorld, variant: str
) -> None:
    assert (await api.get(QUALITY, headers=_bad(gen_world, variant))).status_code == 401


# ---- the platform view ----


@pytest.mark.endpoint("admin_quality", case="success")
@pytest.mark.endpoint("admin_quality", case="wrong_tenant")
async def test_admin_sees_every_tenant_and_filters(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    _admin(gen_world)
    scenario(gen_world, "bharat")
    everyone = await api.get(ADMIN_QUALITY, headers=_hdrs(gen_world, ADMIN))
    assert everyone.status_code == 200, everyone.text
    rows = {t["tenant_id"]: t for t in everyone.json()["by_tenant"]}
    bharat = str(gen_world.tenant("bharat"))
    assert rows[bharat]["listings_generated"] == 2
    one = await api.get(
        ADMIN_QUALITY, headers=_hdrs(gen_world, ADMIN), params={"target_tenant_id": bharat}
    )
    assert one.status_code == 200
    assert [t["tenant_id"] for t in one.json()["by_tenant"]] == [bharat]
    body = one.text
    for word in ("Acme", "Bharat", "@", 'title":', "rationale"):
        assert word not in body


@pytest.mark.endpoint("admin_quality", case="validation")
async def test_admin_bad_input(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    _admin(gen_world)
    hdrs = _hdrs(gen_world, ADMIN)
    assert (await api.get(ADMIN_QUALITY, headers=hdrs, params={"from": "nope"})).status_code == 422
    bad = await api.get(ADMIN_QUALITY, headers=hdrs, params={"target_tenant_id": "x"})
    assert bad.status_code == 422


@pytest.mark.endpoint("admin_quality", case="wrong_role")
async def test_non_admin_403(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    response = await api.get(ADMIN_QUALITY, headers=_hdrs(gen_world, "owner@acme.test"))
    assert response.status_code == 403


@pytest.mark.endpoint("admin_quality", case="missing_auth")
async def test_admin_missing_auth(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    assert (await api.get(ADMIN_QUALITY)).status_code == 401


@pytest.mark.endpoint("admin_quality", case="invalid_token", variant="expired")
@pytest.mark.endpoint("admin_quality", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("admin_quality", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_admin_invalid_token(
    api: httpx.AsyncClient, gen_world: GenWorld, variant: str
) -> None:
    _admin(gen_world)
    assert (await api.get(ADMIN_QUALITY, headers=_bad(gen_world, variant))).status_code == 401


async def test_unknown_target_tenant_reads_empty(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    _admin(gen_world)
    response = await api.get(
        ADMIN_QUALITY,
        headers=_hdrs(gen_world, ADMIN),
        params={"target_tenant_id": str(uuid.uuid4())},
    )
    assert response.status_code == 200 and response.json()["by_tenant"] == []
