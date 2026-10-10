"""S23 T7 (AC2, AC3): `GET /api/v1/analytics` and `/analytics/summary` through the real app as
`app_rw`, over the analytics world seeded into the products world's two tenants."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from .analytics_support import (
    EXPECTED,
    FROM,
    TO,
    AnalyticsWorld,
    build_analytics_world,
    more_rows,
    totals,
)
from .auth_support import PREFIX
from .conftest import LOCAL_HOSTS
from .products_support import bad_bearer, bearer_for

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

RANGE = {"from": FROM.isoformat(), "to": TO.isoformat()}
OPS = {"analytics_get": "/analytics", "analytics_summary": "/analytics/summary"}
ROLES = ["owner", "operator", "reviewer", "admin"]


@pytest.fixture
def an(products_world: Any) -> tuple[Any, AnalyticsWorld]:
    tenants = {slug: products_world.tenant(slug) for slug in ("acme", "bharat")}
    return products_world, build_analytics_world(products_world.conn, tenants)


def _url(op: str) -> str:
    return f"{PREFIX}{OPS[op]}"


def _check_golden(body: dict[str, Any], slug: str) -> None:
    assert body["from"] == FROM.isoformat() and body["to"] == TO.isoformat()
    assert body["counts"] == totals(slug)
    assert [row["marketplace"] for row in body["by_marketplace"]] == list(EXPECTED[slug])
    for row in body["by_marketplace"]:
        assert row["counts"] == EXPECTED[slug][row["marketplace"]]


async def _get(api: httpx.AsyncClient, op: str, headers: dict[str, str], **params: str) -> Any:
    return await api.get(_url(op), headers=headers, params=params)


# ---- success (all four roles), per op ----


@pytest.mark.endpoint("analytics_get", case="success")
@pytest.mark.parametrize("role", ROLES)
async def test_analytics_get_success(an: Any, api: httpx.AsyncClient, role: str) -> None:
    world, _ = an
    response = await _get(api, "analytics_get", bearer_for(world, f"{role}@acme.test"), **RANGE)
    assert response.status_code == 200, response.text
    _check_golden(response.json(), "acme")


@pytest.mark.endpoint("analytics_summary", case="success")
@pytest.mark.parametrize("role", ROLES)
async def test_analytics_summary_success(an: Any, api: httpx.AsyncClient, role: str) -> None:
    world, _ = an
    headers = bearer_for(world, f"{role}@bharat.test")
    response = await _get(api, "analytics_summary", headers, **RANGE)
    assert response.status_code == 200, response.text
    _check_golden(response.json(), "bharat")


async def test_analytics_alias_equals_summary(an: Any, api: httpx.AsyncClient) -> None:
    world, _ = an
    headers = bearer_for(world, "owner@acme.test")
    one = await _get(api, "analytics_get", headers, **RANGE)
    two = await _get(api, "analytics_summary", headers, **RANGE)
    assert one.status_code == two.status_code == 200
    assert one.json() == two.json()


async def test_analytics_default_range_last_30_days(
    an: Any, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime

    from app.services import analytics

    world, _ = an
    monkeypatch.setattr(analytics, "utc_now", lambda: datetime(2026, 9, 30, 8, tzinfo=UTC))
    response = await _get(api, "analytics_get", bearer_for(world, "owner@acme.test"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["from"], body["to"]) == ("2026-09-01", "2026-09-30")
    assert body["counts"] == totals("acme")


# ---- validation ----

BAD_RANGES: dict[str, tuple[dict[str, str], str]] = {
    "from_after_to": ({"from": "2026-09-02", "to": "2026-09-01"}, "from"),
    "span_367": ({"from": "2025-01-01", "to": "2026-01-02"}, "to"),
    "malformed_from": ({"from": "2026/09/01", "to": "2026-09-30"}, "from"),
    "malformed_to": ({"from": "2026-09-01", "to": "yesterday"}, "to"),
    "month_13": ({"from": "2026-13-01"}, "from"),
    "blank": ({"from": "", "to": "2026-09-30"}, "from"),
}


async def _validation(an: Any, api: httpx.AsyncClient, op: str, case: str) -> None:
    world, _ = an
    params, field = BAD_RANGES[case]
    response = await _get(api, op, bearer_for(world, "owner@acme.test"), **params)
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["type"] == "VALIDATION_FAILED"
    assert any(field in detail["loc"] for detail in error["details"]), error


@pytest.mark.endpoint("analytics_get", case="validation")
@pytest.mark.parametrize("case", sorted(BAD_RANGES))
async def test_analytics_get_validation_422(an: Any, api: httpx.AsyncClient, case: str) -> None:
    await _validation(an, api, "analytics_get", case)


@pytest.mark.endpoint("analytics_summary", case="validation")
@pytest.mark.parametrize("case", sorted(BAD_RANGES))
async def test_analytics_summary_validation_422(an: Any, api: httpx.AsyncClient, case: str) -> None:
    await _validation(an, api, "analytics_summary", case)


# ---- auth ----


async def _missing_auth(api: httpx.AsyncClient, op: str) -> None:
    response = await api.get(_url(op), params=RANGE)
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "UNAUTHENTICATED"


@pytest.mark.endpoint("analytics_get", case="missing_auth")
async def test_analytics_get_missing_auth_401(an: Any, api: httpx.AsyncClient) -> None:
    await _missing_auth(api, "analytics_get")


@pytest.mark.endpoint("analytics_summary", case="missing_auth")
async def test_analytics_summary_missing_auth_401(an: Any, api: httpx.AsyncClient) -> None:
    await _missing_auth(api, "analytics_summary")


async def _invalid(an: Any, api: httpx.AsyncClient, op: str, variant: str) -> None:
    world, _ = an
    response = await _get(api, op, bad_bearer(world, variant), **RANGE)
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "UNAUTHENTICATED"


@pytest.mark.endpoint("analytics_get", case="invalid_token", variant="expired")
@pytest.mark.endpoint("analytics_get", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("analytics_get", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_analytics_get_invalid_token_401(
    an: Any, api: httpx.AsyncClient, variant: str
) -> None:
    await _invalid(an, api, "analytics_get", variant)


@pytest.mark.endpoint("analytics_summary", case="invalid_token", variant="expired")
@pytest.mark.endpoint("analytics_summary", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("analytics_summary", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_analytics_summary_invalid_token_401(
    an: Any, api: httpx.AsyncClient, variant: str
) -> None:
    await _invalid(an, api, "analytics_summary", variant)


# ---- wrong tenant: the token's tenant only; a query tenant_id is ignored ----


async def _tenant_isolation(an: Any, api: httpx.AsyncClient, op: str) -> None:
    world, analytics_world = an
    headers = bearer_for(world, "owner@acme.test")
    before = await _get(api, op, headers, **RANGE)
    more_rows(world.conn, world.tenant("bharat"))
    after = await _get(api, op, headers, **RANGE)
    smuggled = await _get(api, op, headers, **RANGE, tenant_id=str(world.tenant("bharat")))
    assert before.status_code == after.status_code == smuggled.status_code == 200
    assert before.json() == after.json() == smuggled.json()
    _check_golden(before.json(), "acme")
    assert analytics_world.tenants["acme"] == world.tenant("acme")


@pytest.mark.endpoint("analytics_get", case="wrong_tenant")
async def test_analytics_tenant_isolation(an: Any, api: httpx.AsyncClient) -> None:
    await _tenant_isolation(an, api, "analytics_get")


@pytest.mark.endpoint("analytics_summary", case="wrong_tenant")
async def test_analytics_summary_tenant_isolation(an: Any, api: httpx.AsyncClient) -> None:
    await _tenant_isolation(an, api, "analytics_summary")


# ---- cases the matrix leaves out, proven anyway ----


async def test_analytics_na_wrong_role_all_roles_allowed(an: Any, api: httpx.AsyncClient) -> None:
    """Every role holds `analytics:read`; a deactivated member is 401."""
    world, _ = an
    for role in ROLES:
        response = await _get(api, "analytics_get", bearer_for(world, f"{role}@acme.test"))
        assert response.status_code == 200, (role, response.text)
    world.conn.execute(
        "UPDATE tenant_memberships SET is_active = false WHERE tenant_id = %s AND user_id = %s",
        [world.tenant("acme"), world.user("operator@acme.test")],
    )
    response = await _get(api, "analytics_get", bearer_for(world, "operator@acme.test"))
    assert response.status_code == 401


async def test_analytics_na_not_found_unknown_subroute_404(an: Any, api: httpx.AsyncClient) -> None:
    world, _ = an
    response = await api.get(
        f"{PREFIX}/analytics/timeseries", headers=bearer_for(world, "owner@acme.test")
    )
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "NOT_FOUND"


async def test_analytics_na_conflict_read_only_405(an: Any, api: httpx.AsyncClient) -> None:
    world, _ = an
    response = await api.post(_url("analytics_get"), headers=bearer_for(world, "owner@acme.test"))
    assert response.status_code == 405
    assert response.json()["error"]["type"] == "METHOD_NOT_ALLOWED"


async def test_analytics_no_idempotency_header_needed(an: Any, api: httpx.AsyncClient) -> None:
    world, _ = an
    headers = {**bearer_for(world, "owner@acme.test"), "Idempotency-Key": "ignored-on-a-read"}
    response = await _get(api, "analytics_summary", headers, **RANGE)
    assert response.status_code == 200


async def test_analytics_inflight_cap(
    an: Any, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two acme requests held open: a third acme request is 429, a bharat request is 200; after
    the release acme is 200 again."""
    from app.services import analytics

    world, _ = an
    gate, entered = asyncio.Event(), asyncio.Semaphore(0)
    original = analytics.summarize
    acme = world.tenant("acme")

    async def held(repo: Any, period: Any) -> dict[str, Any]:
        if repo.tenant_id == acme and not gate.is_set():
            entered.release()
            await gate.wait()
        return await original(repo, period)

    monkeypatch.setattr(analytics, "summarize", held)
    owner = bearer_for(world, "owner@acme.test")
    first = asyncio.create_task(_get(api, "analytics_get", owner, **RANGE))
    second = asyncio.create_task(_get(api, "analytics_get", owner, **RANGE))
    for _ in range(2):
        await asyncio.wait_for(entered.acquire(), timeout=10)
    third = await _get(api, "analytics_get", owner, **RANGE)
    other = await _get(api, "analytics_get", bearer_for(world, "owner@bharat.test"), **RANGE)
    gate.set()
    assert third.status_code == 429
    assert third.json()["error"]["type"] == "RATE_LIMITED"
    assert third.headers.get("Retry-After")
    assert other.status_code == 200
    assert (await first).status_code == (await second).status_code == 200
    assert (await _get(api, "analytics_get", owner, **RANGE)).status_code == 200


async def test_analytics_budget_kill_is_500_without_sql(
    an: Any, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read past its 5 s budget: the one error envelope, 500 INTERNAL_ERROR, no SQL text."""
    from app.repositories.analytics import AnalyticsRepository
    from sqlalchemy import func, literal, select

    world, _ = an
    original = AnalyticsRepository._rows

    async def slow(self: AnalyticsRepository, stmt: Any) -> list[Any]:
        await original(self, select(func.pg_sleep(6), literal("Zq9canary")))
        return []

    monkeypatch.setattr(AnalyticsRepository, "_rows", slow)
    response = await _get(api, "analytics_get", bearer_for(world, "owner@acme.test"), **RANGE)
    assert response.status_code == 500
    body = response.json()
    assert body["error"]["type"] == "INTERNAL_ERROR"
    for leak in ("SELECT", "pg_sleep", "Zq9canary", "SQL", "Traceback"):
        assert leak not in response.text
    monkeypatch.setattr(AnalyticsRepository, "_rows", original)
    assert (
        await _get(api, "analytics_get", bearer_for(world, "owner@acme.test"))
    ).status_code == 200
