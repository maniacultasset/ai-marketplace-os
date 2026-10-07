"""S14b T11 (AC 8): `GET /api/v1/rules`, the platform rules plus the caller's own brand rules,
keyset-paged, for every role (`tenant:read`)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from .auth_support import World
from .conftest import LOCAL_HOSTS
from .products_support import bad_bearer, bearer_for
from .seam_support import CATEGORY, Conn, add_rule, banned, max_length

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
RULES = "/api/v1/rules"
RULE_FIELDS = frozenset(
    {
        "id",
        "marketplace",
        "category",
        "field",
        "rule_type",
        "severity",
        "version",
        "enabled",
        "scope",
    }
)


def _as(world: World, role: str = "operator", slug: str = "acme") -> dict[str, str]:
    return bearer_for(world, f"{role}@{slug}.test")


async def _walk(api: httpx.AsyncClient, headers: dict[str, str], **params: Any) -> list[Any]:
    items: list[Any] = []
    query: dict[str, Any] = dict(params)
    while True:
        response = await api.get(RULES, params=query, headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        items += body["data"]
        if not body["page"]["has_more"]:
            return items
        query = {**params, "cursor": body["page"]["next_cursor"]}


@pytest.mark.endpoint("rules_list", case="success")
async def test_rules_list_success_platform_and_own(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    platform = add_rule(auth_db, max_length())
    own = add_rule(auth_db, banned(["cheap"]), tenant_id=products_world.tenant("acme"))
    flipkart = add_rule(auth_db, max_length(150), marketplace="flipkart", category=None)
    response = await api.get(RULES, headers=_as(products_world))
    assert response.status_code == 200, response.text
    body = response.json()
    by_id = {r["id"]: r for r in body["data"]}
    assert set(by_id) == {str(platform), str(own), str(flipkart)}
    assert all(set(r) == RULE_FIELDS for r in body["data"])
    assert by_id[str(platform)]["scope"] == "platform"
    assert by_id[str(own)]["scope"] == "tenant"
    assert by_id[str(own)]["rule_type"] == "banned_terms"
    assert by_id[str(flipkart)]["category"] is None
    assert body["page"] == {"next_cursor": None, "has_more": False, "limit": 50}
    amazon = await api.get(RULES, params={"marketplace": "amazon"}, headers=_as(products_world))
    assert {r["id"] for r in amazon.json()["data"]} == {str(platform), str(own)}
    kurta = await api.get(RULES, params={"category": CATEGORY}, headers=_as(products_world))
    assert {r["id"] for r in kurta.json()["data"]} == {str(platform), str(own)}


@pytest.mark.endpoint("rules_list", case="success")
async def test_rules_list_keyset_pages(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    made = {str(add_rule(auth_db, max_length(100 + n), version=n + 1)) for n in range(5)}
    items = await _walk(api, _as(products_world), limit=2)
    order = auth_db.execute("SELECT id FROM rules ORDER BY created_at, id").fetchall()
    assert [r["id"] for r in items] == [str(row[0]) for row in order]
    assert {r["id"] for r in items} == made and len(items) == 5


@pytest.mark.endpoint("rules_list", case="validation")
@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 201},
        {"cursor": "eyJ0IjoieCIsImlkIjoieSJ9"},
        {"marketplace": "ebay"},
        {"category": "Bad Category"},
    ],
    ids=["limit0", "limit201", "bad_cursor", "marketplace_ebay", "bad_category"],
)
async def test_rules_list_validation(
    api: httpx.AsyncClient, products_world: World, params: dict[str, Any]
) -> None:
    response = await api.get(RULES, params=params, headers=_as(products_world))
    assert response.status_code == 422
    assert response.json()["error"]["type"] == "VALIDATION_FAILED"


@pytest.mark.endpoint("rules_list", case="missing_auth")
async def test_rules_list_missing_auth(api: httpx.AsyncClient, products_world: World) -> None:
    response = await api.get(RULES)
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "UNAUTHENTICATED"


async def _invalid(api: httpx.AsyncClient, world: World, variant: str) -> None:
    response = await api.get(RULES, headers=bad_bearer(world, variant))
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "UNAUTHENTICATED"


@pytest.mark.endpoint("rules_list", case="invalid_token", variant="expired")
async def test_rules_list_invalid_token_expired(
    api: httpx.AsyncClient, products_world: World
) -> None:
    await _invalid(api, products_world, "expired")


@pytest.mark.endpoint("rules_list", case="invalid_token", variant="bad_signature")
async def test_rules_list_invalid_token_bad_signature(
    api: httpx.AsyncClient, products_world: World
) -> None:
    await _invalid(api, products_world, "bad_signature")


@pytest.mark.endpoint("rules_list", case="invalid_token", variant="wrong_typ")
async def test_rules_list_invalid_token_wrong_typ(
    api: httpx.AsyncClient, products_world: World
) -> None:
    await _invalid(api, products_world, "wrong_typ")


@pytest.mark.endpoint("rules_list", case="wrong_tenant")
async def test_rules_list_wrong_tenant(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    acme_rule = add_rule(auth_db, banned(["cheap"]), tenant_id=products_world.tenant("acme"))
    bharat_rule = add_rule(auth_db, banned(["costly"]), tenant_id=products_world.tenant("bharat"))
    seen = {r["id"] for r in await _walk(api, _as(products_world, slug="bharat"))}
    assert str(acme_rule) not in seen and str(bharat_rule) in seen
    # a cursor minted from acme's page never reveals acme's rows to bharat
    for n in range(3):
        add_rule(auth_db, banned([f"t{n}"]), tenant_id=products_world.tenant("acme"), version=n + 2)
    first = await api.get(RULES, params={"limit": 1}, headers=_as(products_world))
    cursor = first.json()["page"]["next_cursor"]
    assert cursor is not None
    other = await api.get(
        RULES, params={"cursor": cursor}, headers=_as(products_world, slug="bharat")
    )
    assert other.status_code == 200
    assert all(
        r["scope"] == "platform" or r["id"] == str(bharat_rule) for r in other.json()["data"]
    )


@pytest.mark.parametrize("role", ["owner", "operator", "reviewer", "admin"])
async def test_rules_list_na_every_role_200(
    api: httpx.AsyncClient, products_world: World, role: str
) -> None:
    response = await api.get(RULES, headers=_as(products_world, role))
    assert response.status_code == 200, response.text


async def test_rules_list_na_unknown_category_empty_page(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    add_rule(auth_db, max_length())
    response = await api.get(RULES, params={"category": "no_such"}, headers=_as(products_world))
    assert response.status_code == 200
    assert response.json() == {
        "data": [],
        "page": {"next_cursor": None, "has_more": False, "limit": 50},
    }


async def test_rules_list_na_repeat_identical(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    add_rule(auth_db, max_length())
    first = await api.get(RULES, headers=_as(products_world))
    second = await api.get(RULES, headers=_as(products_world))
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
