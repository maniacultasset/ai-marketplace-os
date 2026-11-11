"""Item 17 (client feedback, 11 Nov 2026): `GET /api/v1/listings/{listing_id}/versions` names who
made, edited and approved each version (members of the brand only), with the self-approval mark,
and the history names each person's event. The case matrix of the new op."""

from __future__ import annotations

import uuid

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world
from .products_support import bad_bearer, bearer_for
from .review_support import OPERATOR, OWNER, REVIEWER, edited, generated, item, post_approve

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]
URL = "/api/v1/listings"


def _name(world: GenWorld, email: str, name: str | None) -> None:
    world.conn.execute("UPDATE users SET display_name = %s WHERE email = %s", [name, email])


def _turn_on(world: GenWorld) -> None:
    world.conn.execute(
        "UPDATE tenants SET owner_self_approval = true WHERE id = %s", [world.tenant("acme")]
    )


async def _get(
    api: httpx.AsyncClient,
    world: GenWorld,
    listing_id: object,
    email: str = REVIEWER,
    **params: object,
) -> httpx.Response:
    return await api.get(
        f"{URL}/{listing_id}/versions", headers=bearer_for(world, email), params=params  # type: ignore[arg-type]
    )


@pytest.mark.endpoint("listings_versions", case="success")
async def test_versions_name_maker_editor_and_approver(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    _name(gen_world, OPERATOR, "Ravi Kumar")
    _name(gen_world, OWNER, None)  # no display name: the email instead
    _name(gen_world, REVIEWER, "  Priya Sharma ")
    made = await edited(api, gen_world, await generated(gen_world, OPERATOR), OWNER)
    approved = await post_approve(api, gen_world, REVIEWER, [item(made)])
    assert approved.status_code == 200, approved.text
    response = await _get(api, gen_world, made.listing_id, OPERATOR)
    assert response.status_code == 200, response.text
    edit, first = response.json()["data"]
    operator, owner, reviewer = (gen_world.user(e) for e in (OPERATOR, OWNER, REVIEWER))
    assert first["version"] == 1 and first["origin"] == "generation"
    assert first["written_by"] == "kuinn"
    assert first["generated_by"] == {"user_id": str(operator), "name": "Ravi Kumar"}
    assert first["edited_by"] is None and first["approved_by"] is None
    assert edit["version"] == 2 and edit["origin"] == "edit" and edit["written_by"] == "person"
    assert edit["id"] == str(made.version_id)
    assert edit["generated_by"]["user_id"] == str(operator)  # an edit keeps its parent's maker
    assert edit["edited_by"] == {"user_id": str(owner), "name": OWNER}
    assert edit["approved_by"] == {"user_id": str(reviewer), "name": "Priya Sharma"}
    assert edit["approved_at"] is not None and edit["self_approved"] is False


async def test_versions_mark_a_self_approval(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    _name(gen_world, OWNER, "Asha Mehta")
    made = await generated(gen_world, OWNER)
    gen_world.conn.execute(
        "UPDATE tenant_memberships SET is_active = false, deactivated_at = now() "
        "WHERE user_id = %s",
        [gen_world.user(REVIEWER)],
    )
    _turn_on(gen_world)
    assert (await post_approve(api, gen_world, OWNER, [item(made)])).status_code == 200
    (only,) = (await _get(api, gen_world, made.listing_id, OWNER)).json()["data"]
    owner = {"user_id": str(gen_world.user(OWNER)), "name": "Asha Mehta"}
    assert only["generated_by"] == owner and only["approved_by"] == owner
    assert only["self_approved"] is True


async def test_versions_newest_first_keyset(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    made = await generated(gen_world, OPERATOR)
    made = await edited(api, gen_world, made, OWNER)
    made = await edited(api, gen_world, made, OWNER)
    first = (await _get(api, gen_world, made.listing_id, limit=2)).json()
    assert [v["version"] for v in first["data"]] == [3, 2]
    assert first["page"]["has_more"] is True
    second = (
        await _get(api, gen_world, made.listing_id, limit=2, cursor=first["page"]["next_cursor"])
    ).json()
    assert [v["version"] for v in second["data"]] == [1]
    assert second["page"]["has_more"] is False


async def test_member_names_never_cross_tenants(gen_world: GenWorld) -> None:
    """The names read is the tenant-scoped members select: another tenant's user is unnamed."""
    from app.repositories.uow import tenant_uow

    _name(gen_world, "reviewer@bharat.test", "Bharat Reviewer")
    acme, outsider = gen_world.tenant("acme"), gen_world.user("reviewer@bharat.test")
    async with tenant_uow(acme, user_id=gen_world.user(OWNER)) as uow:
        names = await uow.members.names([outsider, gen_world.user(OWNER)])
    assert outsider not in names and gen_world.user(OWNER) in names


async def test_erased_account_is_unnamed(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    made = await generated(gen_world, OPERATOR)
    gen_world.conn.execute(
        "UPDATE users SET erased_at = now() WHERE id = %s", [gen_world.user(OPERATOR)]
    )
    (only,) = (await _get(api, gen_world, made.listing_id, OWNER)).json()["data"]
    assert only["generated_by"] == {"user_id": str(gen_world.user(OPERATOR)), "name": None}


async def test_history_names_each_person(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    _name(gen_world, REVIEWER, "Priya Sharma")
    made = await generated(gen_world, OPERATOR)
    assert (await post_approve(api, gen_world, REVIEWER, [item(made)])).status_code == 200
    history = await api.get(
        f"{URL}/{made.listing_id}/history", headers=bearer_for(gen_world, OPERATOR)
    )
    assert history.status_code == 200, history.text
    events = {e["action"]: e for e in history.json()["data"]}
    assert events["listing.approved"]["actor_name"] == "Priya Sharma"
    assert all(e["actor_name"] is None for e in history.json()["data"] if e["actor_type"] != "user")


@pytest.mark.endpoint("listings_versions", case="validation")
@pytest.mark.parametrize(
    "bad", [("not-a-uuid", {}), (None, {"limit": 0}), (None, {"cursor": "%%"})]
)
async def test_versions_validation_422(
    api: httpx.AsyncClient, gen_world: GenWorld, bad: tuple[str | None, dict[str, object]]
) -> None:
    listing, params = bad
    response = await _get(api, gen_world, listing or uuid.uuid4(), REVIEWER, **params)
    assert response.status_code == 422


@pytest.mark.endpoint("listings_versions", case="missing_auth")
async def test_versions_missing_auth_401(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    assert (await api.get(f"{URL}/{uuid.uuid4()}/versions")).status_code == 401


async def _invalid(api: httpx.AsyncClient, world: GenWorld, variant: str) -> None:
    response = await api.get(f"{URL}/{uuid.uuid4()}/versions", headers=bad_bearer(world, variant))
    assert response.status_code == 401


@pytest.mark.endpoint("listings_versions", case="invalid_token", variant="expired")
async def test_versions_invalid_token_expired(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    await _invalid(api, gen_world, "expired")


@pytest.mark.endpoint("listings_versions", case="invalid_token", variant="bad_signature")
async def test_versions_invalid_token_bad_signature(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    await _invalid(api, gen_world, "bad_signature")


@pytest.mark.endpoint("listings_versions", case="invalid_token", variant="wrong_typ")
async def test_versions_invalid_token_wrong_typ(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    await _invalid(api, gen_world, "wrong_typ")


@pytest.mark.endpoint("listings_versions", case="wrong_tenant")
async def test_versions_foreign_404(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    headers = {**bearer_for(gen_world, "reviewer@bharat.test"), "X-Request-ID": "req-i17-v-0001"}
    foreign = await api.get(f"{URL}/{made.listing_id}/versions", headers=headers)
    missing = await api.get(f"{URL}/{uuid.uuid4()}/versions", headers=headers)
    assert foreign.status_code == missing.status_code == 404
    assert foreign.content == missing.content


@pytest.mark.endpoint("listings_versions", case="not_found")
async def test_versions_not_found_404(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    response = await _get(api, gen_world, uuid.uuid4())
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "NOT_FOUND"
