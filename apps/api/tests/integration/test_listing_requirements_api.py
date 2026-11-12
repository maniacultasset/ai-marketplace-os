"""Client feedback item 16: `GET /api/v1/listings/{listing_id}/requirements` answers each field's
marketplace limits and rules from the seeded RuleSet and current template the checks run on.
The case matrix of the new op."""

from __future__ import annotations

import uuid

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world
from .products_support import bad_bearer, bearer_for
from .review_support import OPERATOR, REVIEWER, generated

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]
URL = "/api/v1/listings"


async def _get(
    api: httpx.AsyncClient, world: GenWorld, listing_id: object, email: str = REVIEWER
) -> httpx.Response:
    return await api.get(f"{URL}/{listing_id}/requirements", headers=bearer_for(world, email))


@pytest.mark.endpoint("listings_requirements", case="success")
async def test_requirements_amazon_kurta(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    made = await generated(gen_world, OPERATOR)
    response = await _get(api, gen_world, made.listing_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["listing_id"] == str(made.listing_id)
    assert body["marketplace"] == "amazon" and body["category"] == "ethnic_wear_kurta_sets"
    assert body["has_template"] is True and body["rules_version"] >= 1
    fields = {f["path"]: f for f in body["fields"]}
    assert fields["content.title"]["max_chars"] == 200
    assert fields["content.title"]["min_chars"] == 5
    assert "best" in fields["content.title"]["banned_terms"]
    assert fields["content.bullets"]["max_items"] == 5
    assert fields["content.bullets[*]"]["max_chars"] == 500
    assert fields["content.description"]["max_chars"] == 2000
    assert fields["keywords.hidden_terms"]["max_bytes"] == 250
    # a regex list is a plain line, never its pattern
    assert "No links, web addresses, emails or phone numbers." in fields["*"]["rules"]
    assert not any("\\" in t for f in body["fields"] for t in f["banned_terms"])
    levels = {a["key"]: a["level"] for a in body["attributes"]}
    assert levels["color"] == "required"
    order = [a["level"] for a in body["attributes"]]
    assert order == sorted(
        order, key=["required", "conditional", "recommended", "optional", "unsupported"].index
    )


async def test_requirements_flipkart_title(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    made = await generated(gen_world, OPERATOR, marketplace="flipkart")
    body = (await _get(api, gen_world, made.listing_id)).json()
    fields = {f["path"]: f for f in body["fields"]}
    assert body["marketplace"] == "flipkart"
    assert fields["content.title"]["max_chars"] == 150
    assert "keywords.hidden_terms" not in fields


@pytest.mark.endpoint("listings_requirements", case="validation")
async def test_requirements_validation_422(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    assert (await _get(api, gen_world, "not-a-uuid")).status_code == 422


@pytest.mark.endpoint("listings_requirements", case="missing_auth")
async def test_requirements_missing_auth_401(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    assert (await api.get(f"{URL}/{uuid.uuid4()}/requirements")).status_code == 401


async def _invalid(api: httpx.AsyncClient, world: GenWorld, variant: str) -> None:
    response = await api.get(
        f"{URL}/{uuid.uuid4()}/requirements", headers=bad_bearer(world, variant)
    )
    assert response.status_code == 401


@pytest.mark.endpoint("listings_requirements", case="invalid_token", variant="expired")
async def test_requirements_invalid_token_expired(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    await _invalid(api, gen_world, "expired")


@pytest.mark.endpoint("listings_requirements", case="invalid_token", variant="bad_signature")
async def test_requirements_invalid_token_bad_signature(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    await _invalid(api, gen_world, "bad_signature")


@pytest.mark.endpoint("listings_requirements", case="invalid_token", variant="wrong_typ")
async def test_requirements_invalid_token_wrong_typ(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    await _invalid(api, gen_world, "wrong_typ")


@pytest.mark.endpoint("listings_requirements", case="wrong_tenant")
async def test_requirements_foreign_404(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    headers = {**bearer_for(gen_world, "reviewer@bharat.test"), "X-Request-ID": "req-i16-r-0001"}
    foreign = await api.get(f"{URL}/{made.listing_id}/requirements", headers=headers)
    missing = await api.get(f"{URL}/{uuid.uuid4()}/requirements", headers=headers)
    assert foreign.status_code == missing.status_code == 404
    assert foreign.content == missing.content


@pytest.mark.endpoint("listings_requirements", case="not_found")
async def test_requirements_not_found_404(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    response = await _get(api, gen_world, uuid.uuid4())
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "NOT_FOUND"
