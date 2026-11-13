"""Chrome pass 2 (12 Nov): `POST /api/v1/products/{product_id}/archive` and `.../unarchive` as
app_rw through the real app. Archive puts a product away without deleting it: it leaves the
product list (`?archived=true` lists it), its listings are archived, its open questions close and
generate refuses it; its photos stay. Unarchive brings it and its listings back. Refused, with
nothing changed, while a listing is or was with a marketplace (as delete is)."""

from __future__ import annotations

import uuid

import httpx
import pytest

from . import factories as f
from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world, set_status
from .products_support import bad_bearer
from .review_support import ADMIN, OPERATOR, OWNER, REVIEWER, generated

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]
PREFIX = "/api/v1/products"


async def _post(
    api: httpx.AsyncClient, world: GenWorld, product_id: object, what: str, email: str = OWNER
) -> httpx.Response:
    return await api.post(f"{PREFIX}/{product_id}/{what}", headers=world.headers(email))


async def _made(world: GenWorld) -> tuple[uuid.UUID, uuid.UUID]:
    made = await generated(world)
    product = world.scalar("SELECT product_id FROM listings WHERE id = %s", [made.listing_id])
    return uuid.UUID(str(product)), made.listing_id


def _archived(world: GenWorld, product_id: uuid.UUID) -> bool:
    return world.scalar("SELECT archived_at FROM products WHERE id = %s", [product_id]) is not None


async def _listed(api: httpx.AsyncClient, world: GenWorld, **params: str) -> set[str]:
    response = await api.get(PREFIX, headers=world.headers(OWNER), params={"limit": 200, **params})
    assert response.status_code == 200, response.text
    return {p["id"] for p in response.json()["data"]}


# ---- archive: the case matrix ----


@pytest.mark.endpoint("products_archive", case="success")
async def test_archive_puts_the_product_away_and_keeps_it(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    product, listing_id = await _made(gen_world)
    task = f.human_task(gen_world.conn, gen_world.tenant("acme"), product_id=product)
    image = f.image(gen_world.conn, gen_world.tenant("acme"), product, slot="secondary")

    response = await _post(api, gen_world, product, "archive")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == str(product) and body["archived_at"] is not None

    assert _archived(gen_world, product)
    assert gen_world.scalar("SELECT deleted_at FROM products WHERE id = %s", [product]) is None
    row = gen_world.listing(listing_id)
    assert row["archived_at"] is not None and row["last_event"] == "archive"
    assert gen_world.scalar(
        "SELECT status || '/' || cancelled_reason FROM human_tasks WHERE id = %s", [task]
    ) == ("cancelled/listing_archived")
    assert gen_world.scalar("SELECT deleted_at FROM images WHERE id = %s", [image]) is None

    assert str(product) not in await _listed(api, gen_world)
    assert str(product) in await _listed(api, gen_world, archived="true")
    read = await api.get(f"{PREFIX}/{product}", headers=gen_world.headers(OWNER))
    assert read.status_code == 200 and read.json()["archived_at"] is not None
    assert (
        gen_world.count("audit_log", "entity_id = %s AND action = 'product.archived'", [product])
        == 1
    )
    assert (
        gen_world.count("audit_log", "entity_id = %s AND action = 'listing.archived'", [listing_id])
        == 1
    )


@pytest.mark.endpoint("products_archive", case="validation")
async def test_archive_bad_id_422(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    assert (await _post(api, gen_world, "not-a-uuid", "archive")).status_code == 422


@pytest.mark.endpoint("products_archive", case="missing_auth")
async def test_archive_missing_auth_401(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    assert (await api.post(f"{PREFIX}/{uuid.uuid4()}/archive")).status_code == 401


@pytest.mark.endpoint("products_archive", case="invalid_token", variant="expired")
@pytest.mark.endpoint("products_archive", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("products_archive", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_archive_invalid_token_401(
    api: httpx.AsyncClient, gen_world: GenWorld, variant: str
) -> None:
    response = await api.post(
        f"{PREFIX}/{uuid.uuid4()}/archive", headers=bad_bearer(gen_world, variant, OWNER)
    )
    assert response.status_code == 401


@pytest.mark.endpoint("products_archive", case="wrong_role")
@pytest.mark.parametrize("email", [OPERATOR, REVIEWER])
async def test_archive_operator_and_reviewer_403(
    api: httpx.AsyncClient, gen_world: GenWorld, email: str
) -> None:
    product, _ = await _made(gen_world)
    response = await _post(api, gen_world, product, "archive", email)
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "PERMISSION_DENIED"
    assert not _archived(gen_world, product)


@pytest.mark.endpoint("products_archive", case="wrong_tenant")
async def test_archive_foreign_product_404(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    product, listing_id = await _made(gen_world)
    foreign = await _post(api, gen_world, product, "archive", "owner@bharat.test")
    missing = await _post(api, gen_world, uuid.uuid4(), "archive", "owner@bharat.test")
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["error"]["message"] == missing.json()["error"]["message"]
    assert not _archived(gen_world, product)
    assert gen_world.listing(listing_id)["archived_at"] is None


@pytest.mark.endpoint("products_archive", case="not_found")
async def test_archive_unknown_404(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    response = await _post(api, gen_world, uuid.uuid4(), "archive")
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "NOT_FOUND"


@pytest.mark.endpoint("products_archive", case="conflict")
@pytest.mark.parametrize(
    ("status", "code"),
    [("published", "INVALID_STATUS_TRANSITION"), ("publishing", "SUBMISSION_IN_FLIGHT")],
)
async def test_archive_refused_while_a_listing_is_with_a_marketplace(
    api: httpx.AsyncClient, gen_world: GenWorld, status: str, code: str
) -> None:
    product, listing_id = await _made(gen_world)
    set_status(gen_world, listing_id, status)
    response = await _post(api, gen_world, product, "archive")
    assert response.status_code == 409, response.text
    assert response.json()["error"]["type"] == code
    assert not _archived(gen_world, product)
    assert gen_world.listing(listing_id)["archived_at"] is None


# ---- archive: the rest ----


async def test_admin_may_archive_and_a_second_archive_changes_nothing(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    product, _ = await _made(gen_world)
    first = await _post(api, gen_world, product, "archive", ADMIN)
    assert first.status_code == 200, first.text
    again = await _post(api, gen_world, product, "archive")
    assert again.status_code == 200
    assert again.json()["archived_at"] == first.json()["archived_at"]
    assert (
        gen_world.count("audit_log", "entity_id = %s AND action = 'product.archived'", [product])
        == 1
    )


async def test_generate_refuses_an_archived_product(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    product, _ = await _made(gen_world)
    assert (await _post(api, gen_world, product, "archive")).status_code == 200
    response = await api.post(
        "/api/v1/listings/generate",
        headers=gen_world.headers(OWNER),
        json={"product_id": str(product), "marketplace": "amazon"},
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["type"] == "LISTING_ARCHIVED"


# ---- unarchive: the case matrix ----


@pytest.mark.endpoint("products_unarchive", case="success")
async def test_unarchive_brings_the_product_and_its_listings_back(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    product, listing_id = await _made(gen_world)
    assert (await _post(api, gen_world, product, "archive")).status_code == 200

    response = await _post(api, gen_world, product, "unarchive")
    assert response.status_code == 200, response.text
    assert response.json()["archived_at"] is None
    assert not _archived(gen_world, product)
    row = gen_world.listing(listing_id)
    assert row["archived_at"] is None and row["last_event"] == "unarchive"
    assert str(product) in await _listed(api, gen_world)
    assert str(product) not in await _listed(api, gen_world, archived="true")
    assert (
        gen_world.count("audit_log", "entity_id = %s AND action = 'product.unarchived'", [product])
        == 1
    )
    # a product that is not archived answers unchanged
    again = await _post(api, gen_world, product, "unarchive")
    assert again.status_code == 200 and again.json()["archived_at"] is None


@pytest.mark.endpoint("products_unarchive", case="validation")
async def test_unarchive_bad_id_422(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    assert (await _post(api, gen_world, "not-a-uuid", "unarchive")).status_code == 422


@pytest.mark.endpoint("products_unarchive", case="missing_auth")
async def test_unarchive_missing_auth_401(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    assert (await api.post(f"{PREFIX}/{uuid.uuid4()}/unarchive")).status_code == 401


@pytest.mark.endpoint("products_unarchive", case="invalid_token", variant="expired")
@pytest.mark.endpoint("products_unarchive", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("products_unarchive", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_unarchive_invalid_token_401(
    api: httpx.AsyncClient, gen_world: GenWorld, variant: str
) -> None:
    response = await api.post(
        f"{PREFIX}/{uuid.uuid4()}/unarchive", headers=bad_bearer(gen_world, variant, OWNER)
    )
    assert response.status_code == 401


@pytest.mark.endpoint("products_unarchive", case="wrong_role")
@pytest.mark.parametrize("email", [OPERATOR, REVIEWER])
async def test_unarchive_operator_and_reviewer_403(
    api: httpx.AsyncClient, gen_world: GenWorld, email: str
) -> None:
    product, _ = await _made(gen_world)
    assert (await _post(api, gen_world, product, "archive")).status_code == 200
    response = await _post(api, gen_world, product, "unarchive", email)
    assert response.status_code == 403
    assert _archived(gen_world, product)


@pytest.mark.endpoint("products_unarchive", case="wrong_tenant")
async def test_unarchive_foreign_product_404(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    product, _ = await _made(gen_world)
    assert (await _post(api, gen_world, product, "archive")).status_code == 200
    foreign = await _post(api, gen_world, product, "unarchive", "owner@bharat.test")
    assert foreign.status_code == 404
    assert _archived(gen_world, product)


@pytest.mark.endpoint("products_unarchive", case="not_found")
async def test_unarchive_unknown_404(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    response = await _post(api, gen_world, uuid.uuid4(), "unarchive")
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "NOT_FOUND"
