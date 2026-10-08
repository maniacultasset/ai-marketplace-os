"""S18 T18: generate, regenerate and status over HTTP."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world
from .products_support import bad_bearer

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]
GENERATE = "/api/v1/listings/generate"


def _json(product_id: uuid.UUID) -> dict[str, str]:
    return {"product_id": str(product_id), "marketplace": "amazon"}


async def _generate(
    api: httpx.AsyncClient, world: GenWorld, email: str = "operator@acme.test", **kw: Any
) -> httpx.Response:
    product = kw.pop("product", world.product("acme", "KURTA-002"))
    headers = kw.pop("headers", world.headers(email))
    return await api.post(GENERATE, headers=headers, json=kw.pop("json", _json(product)))


@pytest.mark.endpoint("listings_generate", case="success")
async def test_generate_success(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    response = await _generate(api, gen_world)
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["poll_url"] == f"/api/v1/jobs/{body['job_id']}"
    assert gen_world.listing(uuid.UUID(body["listing_id"]))["status"] == "generating"


@pytest.mark.endpoint("listings_generate", case="validation")
async def test_generate_validation(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    response = await api.post(
        GENERATE, headers=gen_world.headers("operator@acme.test"), json={"marketplace": "amazon"}
    )
    assert response.status_code == 422


@pytest.mark.endpoint("listings_generate", case="missing_auth")
async def test_generate_missing_auth(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    response = await api.post(GENERATE, json=_json(gen_world.product("acme", "KURTA-002")))
    assert response.status_code == 401


@pytest.mark.endpoint("listings_generate", case="wrong_role")
async def test_generate_wrong_role(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    response = await _generate(api, gen_world, "reviewer@acme.test")
    assert response.status_code == 403


@pytest.mark.endpoint("listings_generate", case="invalid_token", variant="expired")
@pytest.mark.endpoint("listings_generate", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("listings_generate", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_generate_invalid_token(
    api: httpx.AsyncClient, gen_world: GenWorld, variant: str
) -> None:
    response = await api.post(
        GENERATE,
        headers=bad_bearer(gen_world, variant),
        json=_json(gen_world.product("acme", "KURTA-002")),
    )
    assert response.status_code == 401


@pytest.mark.endpoint("listings_generate", case="not_found")
async def test_generate_not_found(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    response = await _generate(api, gen_world, product=uuid.uuid4())
    assert response.status_code == 404


@pytest.mark.endpoint("listings_generate", case="wrong_tenant")
async def test_generate_wrong_tenant(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    response = await _generate(api, gen_world, headers=gen_world.headers("operator@bharat.test"))
    assert response.status_code == 404


@pytest.mark.endpoint("listings_generate", case="conflict")
async def test_generate_conflict(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    assert (await _generate(api, gen_world)).status_code == 202
    again = await _generate(api, gen_world)
    assert again.status_code == 409
    assert again.json()["error"]["type"] == "LISTING_BUSY"


@pytest.mark.endpoint("listings_status", case="success")
async def test_status_success(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    created = await _generate(api, gen_world)
    listing_id = created.json()["listing_id"]
    response = await api.get(
        f"/api/v1/listings/{listing_id}/status", headers=gen_world.headers("operator@acme.test")
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "generating"


@pytest.mark.endpoint("listings_status", case="not_found")
async def test_status_not_found(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    response = await api.get(
        f"/api/v1/listings/{uuid.uuid4()}/status", headers=gen_world.headers("operator@acme.test")
    )
    assert response.status_code == 404


@pytest.mark.endpoint("listings_status", case="missing_auth")
async def test_status_missing_auth(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    response = await api.get(f"/api/v1/listings/{uuid.uuid4()}/status")
    assert response.status_code == 401


@pytest.mark.endpoint("listings_status", case="validation")
async def test_status_validation(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    response = await api.get(
        "/api/v1/listings/not-a-uuid/status", headers=gen_world.headers("operator@acme.test")
    )
    assert response.status_code == 422


@pytest.mark.endpoint("listings_status", case="invalid_token", variant="expired")
@pytest.mark.endpoint("listings_status", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("listings_status", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_status_invalid_token(
    api: httpx.AsyncClient, gen_world: GenWorld, variant: str
) -> None:
    response = await api.get(
        f"/api/v1/listings/{uuid.uuid4()}/status", headers=bad_bearer(gen_world, variant)
    )
    assert response.status_code == 401


@pytest.mark.endpoint("listings_status", case="wrong_tenant")
async def test_status_wrong_tenant(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    created = await _generate(api, gen_world)
    listing_id = created.json()["listing_id"]
    response = await api.get(
        f"/api/v1/listings/{listing_id}/status", headers=gen_world.headers("operator@bharat.test")
    )
    assert response.status_code == 404


@pytest.mark.endpoint("listings_regenerate", case="success")
async def test_regenerate_success(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    from app.workers.tasks import register_bodies
    from app.workers.wrapper import run_job

    created = await _generate(api, gen_world)
    assert created.status_code == 202
    register_bodies()
    await run_job(created.json()["job_id"])
    listing_id = created.json()["listing_id"]
    response = await api.post(
        f"/api/v1/listings/{listing_id}/regenerate",
        headers=gen_world.headers("reviewer@acme.test"),
        json={"instructions": "shorter title"},
    )
    assert response.status_code == 202, response.text


@pytest.mark.endpoint("listings_regenerate", case="conflict")
async def test_regenerate_conflict(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    listing_id = gen_world.listing_of(gen_world.product("acme", "KURTA-001"))
    response = await api.post(
        f"/api/v1/listings/{listing_id}/regenerate",
        headers=gen_world.headers("reviewer@acme.test"),
        json={},
    )
    assert response.status_code == 409


@pytest.mark.endpoint("listings_regenerate", case="not_found")
async def test_regenerate_not_found(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    response = await api.post(
        f"/api/v1/listings/{uuid.uuid4()}/regenerate",
        headers=gen_world.headers("reviewer@acme.test"),
        json={},
    )
    assert response.status_code == 404


@pytest.mark.endpoint("listings_regenerate", case="wrong_role")
async def test_regenerate_wrong_role(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    listing_id = gen_world.listing_of(gen_world.product("acme", "KURTA-001"))
    response = await api.post(
        f"/api/v1/listings/{listing_id}/regenerate",
        headers=gen_world.headers("admin@acme.test"),
        json={},
    )
    assert response.status_code == 403


@pytest.mark.endpoint("listings_regenerate", case="missing_auth")
async def test_regenerate_missing_auth(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    response = await api.post(f"/api/v1/listings/{uuid.uuid4()}/regenerate", json={})
    assert response.status_code == 401


@pytest.mark.endpoint("listings_regenerate", case="validation")
async def test_regenerate_validation(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    response = await api.post(
        "/api/v1/listings/not-a-uuid/regenerate",
        headers=gen_world.headers("reviewer@acme.test"),
        json={},
    )
    assert response.status_code == 422


@pytest.mark.endpoint("listings_regenerate", case="invalid_token", variant="expired")
@pytest.mark.endpoint("listings_regenerate", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("listings_regenerate", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_regenerate_invalid_token(
    api: httpx.AsyncClient, gen_world: GenWorld, variant: str
) -> None:
    response = await api.post(
        f"/api/v1/listings/{uuid.uuid4()}/regenerate",
        headers=bad_bearer(gen_world, variant, "reviewer@acme.test"),
        json={},
    )
    assert response.status_code == 401


@pytest.mark.endpoint("listings_regenerate", case="wrong_tenant")
async def test_regenerate_wrong_tenant(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    listing_id = gen_world.listing_of(gen_world.product("acme", "KURTA-001"))
    response = await api.post(
        f"/api/v1/listings/{listing_id}/regenerate",
        headers=gen_world.headers("operator@bharat.test"),
        json={},
    )
    assert response.status_code == 404
