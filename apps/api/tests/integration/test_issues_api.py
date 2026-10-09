"""S19 T19 (DW6; AC 13, 15): `GET /api/v1/issues`, the issue inbox, and its case matrix."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world
from .products_support import bad_bearer, bearer_for
from .review_support import REVIEWER, generated, open_issue

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]
URL = "/api/v1/issues"


async def _get(
    api: httpx.AsyncClient, world: GenWorld, email: str = REVIEWER, **params: Any
) -> httpx.Response:
    return await api.get(URL, headers=bearer_for(world, email), params=params)


async def _three(world: GenWorld) -> tuple[Any, list[uuid.UUID]]:
    made = await generated(world)
    ids = [
        await open_issue(world, made, field_path=path)
        for path in ("content.title", "content.description", "content.bullets")
    ]
    return made, ids


@pytest.mark.endpoint("issues_list", case="success")
async def test_issues_inbox(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    made, ids = await _three(gen_world)
    gen_world.conn.execute(
        "UPDATE listing_issues SET status = 'resolved', resolution = 'human_edit', "
        "resolved_at = now() WHERE id = %s",
        [ids[1]],
    )
    first = await _get(api, gen_world, listing_id=str(made.listing_id), limit=1)
    assert first.status_code == 200, first.text
    body = first.json()
    assert [r["id"] for r in body["data"]] == [str(ids[2])]  # newest open first
    assert body["page"]["has_more"] is True
    second = (
        await _get(
            api, gen_world, listing_id=str(made.listing_id), limit=1,
            cursor=body["page"]["next_cursor"],
        )
    ).json()  # fmt: skip
    assert [r["id"] for r in second["data"]] == [str(ids[0])]
    assert second["page"]["has_more"] is False
    row = second["data"][0]
    assert not {"dedupe_key", "rule_id", "validation_run_id", "job_id"} & set(row)
    bharat = (await _get(api, gen_world, "reviewer@bharat.test")).json()
    assert bharat["data"] == []


async def test_issues_filters(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    await open_issue(gen_world, made)
    await open_issue(gen_world, made, source="schema", error_code="FIELD_TOO_LONG")
    base = {"listing_id": str(made.listing_id)}
    assert len((await _get(api, gen_world, **base, source="schema")).json()["data"]) == 1
    codes = (await _get(api, gen_world, **base, error_code="FIELD_TOO_LONG")).json()["data"]
    assert [r["error_code"] for r in codes] == ["FIELD_TOO_LONG"]
    assert (await _get(api, gen_world, **base, marketplace="flipkart")).json()["data"] == []


async def test_issues_status_dismissed(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    issue = await open_issue(gen_world, made)
    gen_world.conn.execute(
        "UPDATE listing_issues SET status = 'dismissed', resolution = 'accepted_warning', "
        "resolved_at = now(), resolved_by_user_id = %s WHERE id = %s",
        [gen_world.user(REVIEWER), issue],
    )
    body = (await _get(api, gen_world, status="dismissed", listing_id=str(made.listing_id))).json()
    assert [r["id"] for r in body["data"]] == [str(issue)]
    assert body["data"][0]["resolution"] == "accepted_warning"
    assert (await _get(api, gen_world, listing_id=str(made.listing_id))).json()["data"] == []


@pytest.mark.endpoint("issues_list", case="validation")
@pytest.mark.parametrize(
    "params",
    [
        {"status": "closed"},
        {"source": "ai_magic"},
        {"error_code": "NOT_A_CODE"},
        {"marketplace": "ebay"},
        {"listing_id": "nope"},
        {"limit": 0},
        {"limit": 201},
        {"cursor": "%%%"},
    ],
)
async def test_issues_bad_query_422(
    api: httpx.AsyncClient, gen_world: GenWorld, params: dict[str, Any]
) -> None:
    response = await _get(api, gen_world, **params)
    assert response.status_code == 422
    assert response.json()["error"]["type"] == "VALIDATION_FAILED"


@pytest.mark.endpoint("issues_list", case="missing_auth")
async def test_issues_missing_auth_401(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    assert (await api.get(URL)).status_code == 401


async def _invalid(api: httpx.AsyncClient, world: GenWorld, variant: str) -> None:
    response = await api.get(URL, headers=bad_bearer(world, variant))
    assert response.status_code == 401


@pytest.mark.endpoint("issues_list", case="invalid_token", variant="expired")
async def test_issues_invalid_token_expired(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    await _invalid(api, gen_world, "expired")


@pytest.mark.endpoint("issues_list", case="invalid_token", variant="bad_signature")
async def test_issues_invalid_token_bad_signature(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    await _invalid(api, gen_world, "bad_signature")


@pytest.mark.endpoint("issues_list", case="invalid_token", variant="wrong_typ")
async def test_issues_invalid_token_wrong_typ(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    await _invalid(api, gen_world, "wrong_typ")


@pytest.mark.endpoint("issues_list", case="wrong_tenant")
async def test_issues_foreign_listing_filter_empty(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world)
    await open_issue(gen_world, made)
    body = (
        await _get(api, gen_world, "reviewer@bharat.test", listing_id=str(made.listing_id))
    ).json()
    assert body["data"] == []


@pytest.mark.parametrize("role", ("owner", "operator", "reviewer", "admin"))
async def test_issues_all_roles_200(api: httpx.AsyncClient, gen_world: GenWorld, role: str) -> None:
    assert (await _get(api, gen_world, f"{role}@acme.test")).status_code == 200
