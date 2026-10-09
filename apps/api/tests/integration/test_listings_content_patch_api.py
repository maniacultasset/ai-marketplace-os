"""S18 T23 + T25: the human content edit, as the service and over HTTP (DW5, AC 11; D13; CP7)."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world, make_generating, set_status
from .products_support import bad_bearer

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]
CLEAN = {
    "title": "Cotton Kurta Set for Women",
    "bullets": ["Soft cotton fabric"],
    "description": "A cotton kurta set with a straight fit.",
}
ATTRS = {"color": "blue", "fabric_type": "cotton"}  # the template's required attributes


def _seeded(world: GenWorld, status: str = "validated") -> tuple[uuid.UUID, uuid.UUID]:
    """The demo KURTA-001 x amazon listing with v1 current, put in `status`."""
    listing_id = world.listing_of(world.product("acme", "KURTA-001"))
    assert listing_id is not None
    set_status(world, listing_id, status)
    version = world.listing(listing_id)["current_version_id"]
    assert version is not None
    return listing_id, version


def _headers(world: GenWorld, version: uuid.UUID | str, email: str = "operator@acme.test") -> Any:
    out = world.headers(email)
    out.pop("Idempotency-Key")
    out["If-Match"] = f'"{version}"'
    return out


async def _patch(
    api: httpx.AsyncClient,
    world: GenWorld,
    listing_id: uuid.UUID,
    version: uuid.UUID | str,
    body: dict[str, Any] | None = None,
    **kw: Any,
) -> httpx.Response:
    headers = kw.pop("headers", None) or _headers(world, version)
    return await api.patch(
        f"/api/v1/listings/{listing_id}/content",
        headers=headers,
        json=body if body is not None else {"content": CLEAN, "attributes": ATTRS},
    )


def _version(world: GenWorld, version_id: Any) -> dict[str, Any]:
    cur = world.conn.execute("SELECT * FROM listing_versions WHERE id = %s", [version_id])
    names = [d.name for d in cur.description or ()]
    row = cur.fetchone()
    assert row is not None
    return dict(zip(names, row, strict=True))


@pytest.mark.endpoint("listings_content_patch", case="success")
async def test_patch_content_versioning(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    listing_id, parent = _seeded(gen_world)
    before = _version(gen_world, parent)
    response = await _patch(api, gen_world, listing_id, parent)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["current_version_id"] != str(parent)
    assert body["content"]["content"]["title"] == CLEAN["title"]
    assert body["status"] == "validated" and body["validation"]["passed"] is True
    new = _version(gen_world, uuid.UUID(body["current_version_id"]))
    assert new["origin"] == "edit" and new["parent_version_id"] == parent
    assert new["generated_by_user_id"] == before["generated_by_user_id"]  # the maker is kept
    assert new["edited_by_user_id"] == gen_world.user("operator@acme.test")
    assert new["changed_paths"] == ["content", "attributes"]
    assert gen_world.count("validation_runs", "listing_version_id = %s", [new["id"]]) == 1


@pytest.mark.parametrize(
    ("extra", "paths"),
    [
        ({"keywords": {"search_terms": ["kurta"]}}, ["content", "keywords", "attributes"]),
        ({}, ["content", "attributes"]),
    ],
)
async def test_patch_changed_paths(
    api: httpx.AsyncClient, gen_world: GenWorld, extra: dict[str, Any], paths: list[str]
) -> None:
    listing_id, parent = _seeded(gen_world)
    response = await _patch(
        api, gen_world, listing_id, parent, {"content": CLEAN, "attributes": ATTRS, **extra}
    )
    assert response.status_code == 200, response.text
    new = _version(gen_world, uuid.UUID(response.json()["current_version_id"]))
    assert new["changed_paths"] == paths


async def test_patch_noop_200_no_version(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    listing_id, parent = _seeded(gen_world)
    first = await _patch(api, gen_world, listing_id, parent)
    current = first.json()["current_version_id"]
    versions = gen_world.count("listing_versions", "listing_id = %s", [listing_id])
    again = await _patch(api, gen_world, listing_id, current)
    assert again.status_code == 200
    assert again.json()["current_version_id"] == current
    assert gen_world.count("listing_versions", "listing_id = %s", [listing_id]) == versions


@pytest.mark.endpoint("listings_content_patch", case="conflict")
async def test_patch_stale_409(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    listing_id, parent = _seeded(gen_world)
    assert (await _patch(api, gen_world, listing_id, parent)).status_code == 200
    stale = await _patch(
        api,
        gen_world,
        listing_id,
        parent,
        {"content": {**CLEAN, "title": "B"}, "attributes": ATTRS},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["type"] == "STALE_VERSION"


@pytest.mark.parametrize(
    ("status", "code"),
    [("created", "INVALID_STATUS_TRANSITION"), ("generating", "LISTING_BUSY")],
)
async def test_patch_status_409(
    api: httpx.AsyncClient, gen_world: GenWorld, status: str, code: str
) -> None:
    listing_id, parent = _seeded(gen_world, status)
    response = await _patch(api, gen_world, listing_id, parent)
    assert response.status_code == 409
    assert response.json()["error"]["type"] == code


async def test_patch_failed_no_version_409(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    from app.workers.tasks import register_bodies
    from app.workers.wrapper import run_job

    accepted = await make_generating(gen_world, instructions="stub:failed")
    register_bodies()
    await run_job(str(accepted.job_id))
    response = await _patch(api, gen_world, accepted.listing_id, uuid.uuid4())
    assert response.status_code == 409
    assert response.json()["error"]["type"] == "INVALID_STATUS_TRANSITION"


@pytest.mark.parametrize(
    ("title", "status", "reason"),
    [
        ("Cotton Kurta Set for Women", "validated", None),
        ("Cotton Kurta Set " * 14, "blocked", "validation_errors"),
    ],
)
async def test_patch_recheck_status(
    api: httpx.AsyncClient,
    gen_world: GenWorld,
    title: str,
    status: str,
    reason: str | None,
) -> None:
    listing_id, parent = _seeded(gen_world, "needs_review")
    response = await _patch(
        api,
        gen_world,
        listing_id,
        parent,
        {"content": {**CLEAN, "title": title.strip()}, "attributes": ATTRS},
    )
    assert response.status_code == 200, response.text
    row = gen_world.listing(listing_id)
    assert (row["status"], row["status_reason"]) == (status, reason)


async def test_patch_voids_approval(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    listing_id, parent = _seeded(gen_world)
    with gen_world.conn.transaction():
        gen_world.conn.execute("SET LOCAL session_replication_role = replica")
        gen_world.conn.execute(
            "UPDATE listings SET status = 'approved', approved_version_id = current_version_id "
            "WHERE id = %s",
            [listing_id],
        )
    response = await _patch(api, gen_world, listing_id, parent)
    assert response.status_code == 200, response.text
    row = gen_world.listing(listing_id)
    assert row["approved_version_id"] is None and row["status"] == "validated"


async def test_patch_concurrent_same_if_match_one_wins(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    listing_id, parent = _seeded(gen_world)
    a, b = await asyncio.gather(
        _patch(
            api,
            gen_world,
            listing_id,
            parent,
            {"content": {**CLEAN, "title": "A one"}, "attributes": ATTRS},
        ),
        _patch(
            api,
            gen_world,
            listing_id,
            parent,
            {"content": {**CLEAN, "title": "B two"}, "attributes": ATTRS},
        ),
    )
    assert sorted([a.status_code, b.status_code]) == [200, 409]


async def test_patch_unicode_content(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    listing_id, parent = _seeded(gen_world)
    title = "सूती कुर्ता सेट"
    response = await _patch(
        api, gen_world, listing_id, parent, {"content": {**CLEAN, "title": title}}
    )
    assert response.status_code == 200, response.text
    assert response.json()["content"]["content"]["title"] == title


async def test_patch_nested_unknown_key_422(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    """Invariant 4 (S19 AC 8): an unknown key inside `content` is refused, never dropped with a
    200 that tells the editor the whole edit was kept."""
    listing_id, parent = _seeded(gen_world)
    response = await _patch(
        api, gen_world, listing_id, parent, {"content": {**CLEAN, "seeded_extra": "x"}}
    )
    assert response.status_code == 422, response.text
    assert gen_world.listing(listing_id)["current_version_id"] == parent


@pytest.mark.endpoint("listings_content_patch", case="validation")
@pytest.mark.parametrize(
    "case",
    [
        "blank_title",
        "501_char_title",
        "51_terms",
        "if_match_missing",
        "unquoted",
        "weak",
        "not_uuid",
        "attr_257_chars",
        "attr_129_keys",
        "attr_list_item_257",
        "attr_key_bad_shape",
    ],
)
async def test_patch_invalid_422(api: httpx.AsyncClient, gen_world: GenWorld, case: str) -> None:
    listing_id, parent = _seeded(gen_world)
    headers = _headers(gen_world, parent)
    body: dict[str, Any] = {"content": CLEAN}
    if case == "blank_title":
        body = {"content": {**CLEAN, "title": " "}}
    elif case == "501_char_title":
        body = {"content": {**CLEAN, "title": "x" * 501}}
    elif case == "51_terms":
        body = {"content": CLEAN, "keywords": {"search_terms": [f"t{i}" for i in range(51)]}}
    elif case == "attr_257_chars":
        body = {"content": CLEAN, "attributes": {**ATTRS, "color": "x" * 257}}
    elif case == "attr_129_keys":
        body = {"content": CLEAN, "attributes": {**ATTRS, **{f"k{i}": "v" for i in range(127)}}}
    elif case == "attr_list_item_257":
        body = {"content": CLEAN, "attributes": {**ATTRS, "sizes": ["s", "y" * 257]}}
    elif case == "attr_key_bad_shape":
        # outside `^[a-z0-9_]{1,64}$`. A well-formed key the template lacks is not refused: it is
        # stored and judged by the rules re-check, as generation may store it.
        body = {"content": CLEAN, "attributes": {**ATTRS, "Bad-Key": "x"}}
    elif case == "if_match_missing":
        headers.pop("If-Match")
    elif case == "not_uuid":
        headers["If-Match"] = '"' + "-" * 36 + '"'
    elif case == "unquoted":
        headers["If-Match"] = str(parent)
    else:
        headers["If-Match"] = f'W/"{parent}"'
    response = await _patch(api, gen_world, listing_id, parent, body, headers=headers)
    assert response.status_code == 422, response.text
    assert gen_world.count("listing_versions", "listing_id = %s", [listing_id]) == 1


@pytest.mark.endpoint("listings_content_patch", case="missing_auth")
async def test_patch_missing_auth(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    listing_id, parent = _seeded(gen_world)
    response = await api.patch(
        f"/api/v1/listings/{listing_id}/content",
        headers={"If-Match": f'"{parent}"'},
        json={"content": CLEAN},
    )
    assert response.status_code == 401


@pytest.mark.endpoint("listings_content_patch", case="invalid_token", variant="expired")
@pytest.mark.endpoint("listings_content_patch", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("listings_content_patch", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_patch_invalid_token(
    api: httpx.AsyncClient, gen_world: GenWorld, variant: str
) -> None:
    listing_id, parent = _seeded(gen_world)
    headers = {**bad_bearer(gen_world, variant), "If-Match": f'"{parent}"'}
    response = await _patch(api, gen_world, listing_id, parent, headers=headers)
    assert response.status_code == 401


@pytest.mark.endpoint("listings_content_patch", case="wrong_role")
@pytest.mark.parametrize("email", ["reviewer@acme.test", "admin@acme.test"])
async def test_patch_wrong_role(api: httpx.AsyncClient, gen_world: GenWorld, email: str) -> None:
    listing_id, parent = _seeded(gen_world)
    headers = _headers(gen_world, parent, email)
    response = await _patch(api, gen_world, listing_id, parent, headers=headers)
    assert response.status_code == 403


@pytest.mark.endpoint("listings_content_patch", case="not_found")
async def test_patch_not_found(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    response = await _patch(api, gen_world, uuid.uuid4(), uuid.uuid4())
    assert response.status_code == 404


@pytest.mark.endpoint("listings_content_patch", case="wrong_tenant")
async def test_patch_wrong_tenant(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    listing_id, parent = _seeded(gen_world)
    headers = _headers(gen_world, parent, "operator@bharat.test")
    response = await _patch(api, gen_world, listing_id, parent, headers=headers)
    assert response.status_code == 404
    assert gen_world.count("listing_versions", "listing_id = %s", [listing_id]) == 1
