"""S19 T20 (AC 13, 15; invariant 2): `POST /api/v1/issues/{issue_id}/dismiss` accepts an open
warning (reviewer or owner), audited; an error or a closed issue is 409 before SQL."""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world
from .products_support import bad_bearer, bearer_for, statements
from .review_support import OPERATOR, REVIEWER, generated, open_issue

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]


async def _dismiss(
    api: httpx.AsyncClient,
    world: GenWorld,
    issue: object,
    email: str = REVIEWER,
    key: str | None = None,
) -> httpx.Response:
    headers = {**bearer_for(world, email), "Idempotency-Key": key or str(uuid.uuid4())}
    return await api.post(f"/api/v1/issues/{issue}/dismiss", headers=headers)


def _status(world: GenWorld, issue: uuid.UUID) -> str:
    return str(world.scalar("SELECT status FROM listing_issues WHERE id = %s", [issue]))


@pytest.mark.endpoint("issues_dismiss", case="success")
async def test_issue_dismiss_warning_200_audited(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    issue = await open_issue(gen_world, await generated(gen_world))
    response = await _dismiss(api, gen_world, issue)
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["status"], body["resolution"], body["resolved_by_user_id"]) == (
        "dismissed",
        "accepted_warning",
        str(gen_world.user(REVIEWER)),
    )
    audit = gen_world.conn.execute(
        "SELECT actor_user_id, before, after FROM audit_log "
        "WHERE entity_id = %s AND action = 'listing_issue.dismissed'",
        [issue],
    ).fetchone()
    assert audit == (
        gen_world.user(REVIEWER),
        {"status": "open"},
        {"status": "dismissed", "resolution": "accepted_warning"},
    )


async def test_issue_dismiss_owner_200(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    issue = await open_issue(gen_world, await generated(gen_world))
    assert (await _dismiss(api, gen_world, issue, "owner@acme.test")).status_code == 200


@pytest.mark.endpoint("issues_dismiss", case="conflict")
async def test_issue_dismiss_error_409(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    issue = await open_issue(gen_world, await generated(gen_world), severity="error")
    with statements() as seen:
        response = await _dismiss(api, gen_world, issue)
    assert response.status_code == 409
    assert response.json()["error"]["type"] == "INVALID_STATUS_TRANSITION"
    assert not [s for s in seen if s.lstrip().upper().startswith("UPDATE LISTING_ISSUES")]
    assert _status(gen_world, issue) == "open"


async def test_issue_dismiss_closed_409(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    issue = await open_issue(gen_world, await generated(gen_world))
    assert (await _dismiss(api, gen_world, issue)).status_code == 200
    again = await _dismiss(api, gen_world, issue)
    assert again.status_code == 409


@pytest.mark.endpoint("issues_dismiss", case="wrong_role")
@pytest.mark.parametrize("email", [OPERATOR, "admin@acme.test"])
async def test_issue_dismiss_wrong_role_403(
    api: httpx.AsyncClient, gen_world: GenWorld, email: str
) -> None:
    issue = await open_issue(gen_world, await generated(gen_world))
    response = await _dismiss(api, gen_world, issue, email)
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "PERMISSION_DENIED"
    assert _status(gen_world, issue) == "open"


async def test_issue_dismiss_replay(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    issue = await open_issue(gen_world, await generated(gen_world))
    key = str(uuid.uuid4())
    first = await _dismiss(api, gen_world, issue, key=key)
    again = await _dismiss(api, gen_world, issue, key=key)
    assert again.status_code == 200 and again.content == first.content
    assert (
        gen_world.count(
            "audit_log", "entity_id = %s AND action = 'listing_issue.dismissed'", [issue]
        )
        == 1
    )


async def test_issue_dismiss_other_issue_same_key_409(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world)
    one = await open_issue(gen_world, made)
    two = await open_issue(gen_world, made, field_path="content.description")
    key = str(uuid.uuid4())
    assert (await _dismiss(api, gen_world, one, key=key)).status_code == 200
    other = await _dismiss(api, gen_world, two, key=key)
    assert other.status_code == 409
    assert other.json()["error"]["type"] == "IDEMPOTENCY_KEY_REUSED"
    assert _status(gen_world, two) == "open"


@pytest.mark.endpoint("issues_dismiss", case="validation")
@pytest.mark.parametrize("variant", ["bad_uuid", "no_key"])
async def test_issue_dismiss_validation_422(
    api: httpx.AsyncClient, gen_world: GenWorld, variant: str
) -> None:
    if variant == "bad_uuid":
        response = await _dismiss(api, gen_world, "not-a-uuid")
    else:
        response = await api.post(
            f"/api/v1/issues/{uuid.uuid4()}/dismiss", headers=bearer_for(gen_world, REVIEWER)
        )
    assert response.status_code == 422


@pytest.mark.endpoint("issues_dismiss", case="missing_auth")
async def test_issue_dismiss_missing_auth_401(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    response = await api.post(
        f"/api/v1/issues/{uuid.uuid4()}/dismiss", headers={"Idempotency-Key": "k-1"}
    )
    assert response.status_code == 401


async def _invalid(api: httpx.AsyncClient, world: GenWorld, variant: str) -> None:
    headers = {**bad_bearer(world, variant, REVIEWER), "Idempotency-Key": str(uuid.uuid4())}
    response = await api.post(f"/api/v1/issues/{uuid.uuid4()}/dismiss", headers=headers)
    assert response.status_code == 401


@pytest.mark.endpoint("issues_dismiss", case="invalid_token", variant="expired")
async def test_issue_dismiss_invalid_token_expired(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    await _invalid(api, gen_world, "expired")


@pytest.mark.endpoint("issues_dismiss", case="invalid_token", variant="bad_signature")
async def test_issue_dismiss_invalid_token_bad_signature(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    await _invalid(api, gen_world, "bad_signature")


@pytest.mark.endpoint("issues_dismiss", case="invalid_token", variant="wrong_typ")
async def test_issue_dismiss_invalid_token_wrong_typ(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    await _invalid(api, gen_world, "wrong_typ")


@pytest.mark.endpoint("issues_dismiss", case="wrong_tenant")
async def test_issue_dismiss_foreign_404(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    issue = await open_issue(gen_world, await generated(gen_world))
    headers_key = "req-s19-dismiss-x"
    foreign = await _dismiss(api, gen_world, issue, "reviewer@bharat.test", key=headers_key + "1")
    missing = await _dismiss(
        api, gen_world, uuid.uuid4(), "reviewer@bharat.test", key=headers_key + "2"
    )
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["error"]["message"] == missing.json()["error"]["message"]
    assert _status(gen_world, issue) == "open"


@pytest.mark.endpoint("issues_dismiss", case="not_found")
async def test_issue_dismiss_not_found_404(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    response = await _dismiss(api, gen_world, uuid.uuid4())
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "NOT_FOUND"


async def test_issue_dismiss_concurrent_one_wins(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    issue = await open_issue(gen_world, await generated(gen_world))
    a, b = await asyncio.gather(
        _dismiss(api, gen_world, issue, REVIEWER),
        _dismiss(api, gen_world, issue, "owner@acme.test"),
    )
    assert sorted([a.status_code, b.status_code]) == [200, 409]
    assert (
        gen_world.count(
            "audit_log", "entity_id = %s AND action = 'listing_issue.dismissed'", [issue]
        )
        == 1
    )
