"""S18 T24 + T25: validate on demand, as the service and over HTTP (DW6, AC 12; D14; CP4, CP7)."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from . import factories as f
from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world, set_status
from .products_support import bad_bearer

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]
VALIDATE = "/api/v1/listings/validate"


def _seeded(
    world: GenWorld, status: str = "validated", reason: str | None = None
) -> tuple[uuid.UUID, uuid.UUID]:
    """The demo KURTA-001 x amazon listing; its seeded v1 title is longer than Amazon allows."""
    listing_id = world.listing_of(world.product("acme", "KURTA-001"))
    assert listing_id is not None
    set_status(world, listing_id, status, reason)
    version = world.listing(listing_id)["current_version_id"]
    assert version is not None
    return listing_id, version


def _clean_version(world: GenWorld, listing_id: uuid.UUID) -> uuid.UUID:
    """A clean current version, written by the admin login (guards off for the switch)."""
    acme = world.tenant("acme")
    version = f.version(
        world.conn,
        acme,
        listing_id,
        version=99,
        content_json={
            "content": {
                "title": "Cotton Kurta Set",
                "bullets": ["Soft cotton"],
                "description": "A cotton kurta set.",
            },
            "attributes": {"color": "blue", "fabric_type": "cotton"},
        },
    )
    with world.conn.transaction():
        world.conn.execute("SET LOCAL session_replication_role = replica")
        world.conn.execute(
            "UPDATE listings SET current_version_id = %s WHERE id = %s", [version, listing_id]
        )
    return version


async def _validate(
    api: httpx.AsyncClient,
    world: GenWorld,
    listing_id: uuid.UUID,
    version: uuid.UUID | None = None,
    email: str = "operator@acme.test",
    **kw: Any,
) -> httpx.Response:
    body: dict[str, Any] = {"listing_id": str(listing_id)}
    if version is not None:
        body["listing_version_id"] = str(version)
    headers = kw.pop("headers", None) or world.headers(email)
    return await api.post(VALIDATE, headers=headers, json=kw.pop("json", body))


def _runs(world: GenWorld, version: uuid.UUID) -> int:
    return world.count("validation_runs", "listing_version_id = %s", [version])


@pytest.mark.endpoint("listings_validate", case="success")
async def test_validate_200_failed(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    listing_id, version = _seeded(gen_world)
    response = await _validate(api, gen_world, listing_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["passed"] is False and body["blocking_count"] >= 1
    assert body["stages"][-1]["source"] == "rules_engine"
    assert body["listing_version_id"] == str(version)


async def test_validate_equal_pins_no_new_row(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    listing_id, version = _seeded(gen_world, "needs_review")
    first = await _validate(api, gen_world, listing_id)
    runs = _runs(gen_world, version)
    again = await _validate(api, gen_world, listing_id)
    assert again.status_code == 200
    assert _runs(gen_world, version) == runs
    assert again.json()["stages"][-1] == first.json()["stages"][-1]


async def test_validate_facts_change_new_row(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    listing_id, version = _seeded(gen_world, "needs_review")
    await _validate(api, gen_world, listing_id)
    runs = _runs(gen_world, version)
    gen_world.conn.execute(
        "UPDATE products SET title = title || ' new' WHERE id = %s",
        [gen_world.product("acme", "KURTA-001")],
    )
    await _validate(api, gen_world, listing_id)
    assert _runs(gen_world, version) == runs + 1


@pytest.mark.parametrize(
    ("start", "reason", "clean", "to"),
    [
        ("validated", None, False, "blocked"),
        ("needs_review", None, True, "validated"),
        ("approved", None, False, "blocked"),
        ("blocked", "validation_errors", True, "validated"),
        ("blocked", "corrections_exhausted", True, "validated"),
    ],
)
async def test_validate_revalidate_moves(
    api: httpx.AsyncClient,
    gen_world: GenWorld,
    start: str,
    reason: str | None,
    clean: bool,
    to: str,
) -> None:
    listing_id, _ = _seeded(gen_world, start, reason)
    if clean:
        _clean_version(gen_world, listing_id)
    if start == "approved":
        gen_world.conn.execute(
            "UPDATE listings SET approved_version_id = current_version_id WHERE id = %s",
            [listing_id],
        )
    response = await _validate(api, gen_world, listing_id)
    assert response.status_code == 200, response.text
    row = gen_world.listing(listing_id)
    assert row["status"] == to
    if start == "approved":
        assert row["approved_version_id"] is None  # the findings void the approval


async def test_validate_blocked_rejected_not_moved(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    listing_id, _ = _seeded(gen_world, "blocked", "rejected")
    _clean_version(gen_world, listing_id)
    response = await _validate(api, gen_world, listing_id)
    assert response.status_code == 200 and response.json()["passed"] is True
    row = gen_world.listing(listing_id)
    assert (row["status"], row["status_reason"]) == ("blocked", "rejected")  # CP4


async def test_validate_old_version_run_only(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    listing_id, old = _seeded(gen_world, "needs_review")
    _clean_version(gen_world, listing_id)
    response = await _validate(api, gen_world, listing_id, old)
    assert response.status_code == 200 and response.json()["passed"] is False
    assert gen_world.listing(listing_id)["status"] == "needs_review"
    assert _runs(gen_world, old) >= 1


async def test_validate_published_run_only(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    listing_id, version = _seeded(gen_world, "published")
    response = await _validate(api, gen_world, listing_id)
    assert response.status_code == 200
    assert gen_world.listing(listing_id)["status"] == "published"
    assert _runs(gen_world, version) >= 1


async def test_validate_other_listing_version_404(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    listing_id, _ = _seeded(gen_world)
    other = f.listing(gen_world.conn, gen_world.tenant("acme"))
    other_version = f.version(gen_world.conn, gen_world.tenant("acme"), other)
    response = await _validate(api, gen_world, listing_id, other_version)
    assert response.status_code == 404


async def test_validate_no_version_404(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    listing = f.listing(gen_world.conn, gen_world.tenant("acme"))
    response = await _validate(api, gen_world, listing)
    assert response.status_code == 404


@pytest.mark.endpoint("listings_validate", case="validation")
@pytest.mark.parametrize(
    "body", [{"listing_id": "nope"}, {"listing_id": str(uuid.uuid4()), "extra": 1}, {}]
)
async def test_validate_422(
    api: httpx.AsyncClient, gen_world: GenWorld, body: dict[str, Any]
) -> None:
    response = await api.post(VALIDATE, headers=gen_world.headers("operator@acme.test"), json=body)
    assert response.status_code == 422


@pytest.mark.endpoint("listings_validate", case="missing_auth")
async def test_validate_missing_auth(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    listing_id, _ = _seeded(gen_world)
    response = await api.post(VALIDATE, json={"listing_id": str(listing_id)})
    assert response.status_code == 401


@pytest.mark.endpoint("listings_validate", case="invalid_token", variant="expired")
@pytest.mark.endpoint("listings_validate", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("listings_validate", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_validate_invalid_token(
    api: httpx.AsyncClient, gen_world: GenWorld, variant: str
) -> None:
    listing_id, _ = _seeded(gen_world)
    response = await _validate(api, gen_world, listing_id, headers=bad_bearer(gen_world, variant))
    assert response.status_code == 401


@pytest.mark.endpoint("listings_validate", case="wrong_role")
@pytest.mark.parametrize("email", ["reviewer@acme.test", "admin@acme.test"])
async def test_validate_wrong_role(api: httpx.AsyncClient, gen_world: GenWorld, email: str) -> None:
    listing_id, _ = _seeded(gen_world)
    response = await _validate(api, gen_world, listing_id, email=email)
    assert response.status_code == 403


@pytest.mark.endpoint("listings_validate", case="not_found")
async def test_validate_not_found(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    response = await _validate(api, gen_world, uuid.uuid4())
    assert response.status_code == 404


@pytest.mark.endpoint("listings_validate", case="wrong_tenant")
async def test_validate_wrong_tenant(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    listing_id, version = _seeded(gen_world)
    runs = _runs(gen_world, version)
    response = await _validate(api, gen_world, listing_id, email="operator@bharat.test")
    assert response.status_code == 404
    assert _runs(gen_world, version) == runs
