"""S19 T6 (AC 12, 15; D8): `GET /api/v1/listings/{listing_id}` with its `ETag`, and its case
matrix."""

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
    return await api.get(f"{URL}/{listing_id}", headers=bearer_for(world, email))


@pytest.mark.endpoint("listings_get", case="success")
async def test_listing_detail_content_and_validation(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world)
    response = await _get(api, gen_world, made.listing_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == str(made.listing_id)
    assert body["current_version_id"] == str(made.version_id)
    assert body["status"] == "validated"
    assert body["content"]["content"]["title"]
    assert body["validation"]["passed"] is True
    assert body["validation"]["listing_version_id"] == str(made.version_id)


async def test_listing_detail_etag_is_current_version(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world)
    response = await _get(api, gen_world, made.listing_id)
    assert response.headers["ETag"] == f'"{made.version_id}"'


async def test_listing_detail_no_version_no_etag(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    from .generation_support import make_generating
    from .review_support import new_product

    accepted = await make_generating(gen_world, sku=await new_product(gen_world))
    response = await _get(api, gen_world, accepted.listing_id)
    assert response.status_code == 200
    assert "ETag" not in response.headers
    assert response.json()["content"] is None


async def test_listing_detail_patch_response_etag_matches_get(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world)
    headers = {**bearer_for(gen_world, OPERATOR), "If-Match": f'"{made.version_id}"'}
    patched = await api.patch(
        f"{URL}/{made.listing_id}/content",
        headers=headers,
        json={
            "content": {
                "title": "Cotton Kurta Set for Women",
                "bullets": ["Soft cotton fabric"],
                "description": "A cotton kurta set with a straight fit.",
            }
        },
    )
    assert patched.status_code == 200, patched.text
    new = patched.json()["current_version_id"]
    assert patched.headers["ETag"] == f'"{new}"'
    assert (await _get(api, gen_world, made.listing_id)).headers["ETag"] == f'"{new}"'


@pytest.mark.endpoint("listings_get", case="validation")
async def test_listing_detail_bad_uuid_422(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    response = await _get(api, gen_world, "not-a-uuid")
    assert response.status_code == 422
    assert response.json()["error"]["type"] == "VALIDATION_FAILED"


@pytest.mark.endpoint("listings_get", case="missing_auth")
async def test_listing_detail_missing_auth_401(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    response = await api.get(f"{URL}/{uuid.uuid4()}")
    assert response.status_code == 401


async def _invalid(api: httpx.AsyncClient, world: GenWorld, variant: str) -> None:
    response = await api.get(f"{URL}/{uuid.uuid4()}", headers=bad_bearer(world, variant))
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "UNAUTHENTICATED"


@pytest.mark.endpoint("listings_get", case="invalid_token", variant="expired")
async def test_listing_detail_invalid_token_expired(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    await _invalid(api, gen_world, "expired")


@pytest.mark.endpoint("listings_get", case="invalid_token", variant="bad_signature")
async def test_listing_detail_invalid_token_bad_signature(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    await _invalid(api, gen_world, "bad_signature")


@pytest.mark.endpoint("listings_get", case="invalid_token", variant="wrong_typ")
async def test_listing_detail_invalid_token_wrong_typ(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    await _invalid(api, gen_world, "wrong_typ")


@pytest.mark.endpoint("listings_get", case="wrong_tenant")
async def test_listing_detail_foreign_and_random_404_identical(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world)
    headers = {**bearer_for(gen_world, "reviewer@bharat.test"), "X-Request-ID": "req-s19-x-0001"}
    foreign = await api.get(f"{URL}/{made.listing_id}", headers=headers)
    missing = await api.get(f"{URL}/{uuid.uuid4()}", headers=headers)
    assert foreign.status_code == missing.status_code == 404
    assert foreign.content == missing.content
    assert "ETag" not in foreign.headers


@pytest.mark.endpoint("listings_get", case="not_found")
async def test_listing_detail_not_found_404(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    response = await _get(api, gen_world, uuid.uuid4())
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "NOT_FOUND"


@pytest.mark.parametrize("role", ("owner", "operator", "reviewer", "admin"))
async def test_listing_detail_all_roles_200(
    api: httpx.AsyncClient, gen_world: GenWorld, role: str
) -> None:
    made = await generated(gen_world)
    assert (await _get(api, gen_world, made.listing_id, f"{role}@acme.test")).status_code == 200
