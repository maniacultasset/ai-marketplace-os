"""SI5 (ADR-0029): team invites by copied link through the real app as app_rw. Part 1: create,
list and revoke (AC1, AC2, AC7). Part 2: lookup, accept with a password and join (AC3, AC5, AC6)."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any

import httpx
import jwt
import pytest
from tests.conftest import CI_JWT_SECRET

from .auth_support import PREFIX, Conn, World, audit_rows, bearer, build_world, tokens_for
from .conftest import LOCAL_HOSTS

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

INVITES = f"{PREFIX}/invites"


@pytest.fixture
def world(auth_db: Conn) -> World:
    return build_world(auth_db)


def _as(world: World, email: str) -> dict[str, str]:
    slug = email.split("@", 1)[1].split(".", 1)[0]
    return bearer(tokens_for(world, email, slug))


async def _invite(
    api: httpx.AsyncClient,
    world: World,
    email: str,
    role: str = "reviewer",
    caller: str = "owner@acme.test",
) -> httpx.Response:
    return await api.post(INVITES, json={"email": email, "role": role}, headers=_as(world, caller))


def _fragment(url: str) -> str:
    assert "#token=" in url, url
    return url.split("#token=", 1)[1]


def _stored_hash(conn: Conn, invite_id: str) -> bytes:
    row = conn.execute("SELECT token_hash FROM invitations WHERE id = %s", [invite_id]).fetchone()
    assert row is not None
    return bytes(row[0])


def _token(world: World, email: str, **overrides: Any) -> str:
    now = int(time.time())
    claims = {
        "sub": str(world.user(email)),
        "tid": str(world.tenant("acme")),
        "roles": [email.split("@")[0]],
        "email": email,
        "typ": "access",
        "jti": str(uuid.uuid4()),
        "iss": "http://localhost:8000",
        "aud": "ai-marketplace-os",
        "iat": now,
        "exp": now + 600,
        **overrides,
    }
    return jwt.encode(claims, CI_JWT_SECRET, algorithm="HS256")


def _resigned(token: str) -> str:
    return jwt.encode(
        jwt.decode(token, options={"verify_signature": False}),
        "another_secret_that_is_at_least_32_chars",
        algorithm="HS256",
    )


# ---- create (AC1, AC2) ----


@pytest.mark.endpoint("invites_create", case="success")
async def test_create_returns_the_link_once(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    response = await _invite(api, world, "meera@acme.test")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["invite_url"].startswith("http://localhost:3000/invite#token=")
    token = _fragment(body["invite_url"])
    assert len(token) == 43
    assert hashlib.sha256(token.encode()).digest() == _stored_hash(auth_db, body["invite"]["id"])
    invite = body["invite"]
    assert invite["status"] == "pending" and invite["role"] == "reviewer"
    assert invite["invited_by_user_id"] == str(world.user("owner@acme.test"))
    listed = await api.get(INVITES, headers=_as(world, "owner@acme.test"))
    assert listed.status_code == 200
    assert [i["status"] for i in listed.json()["data"]] == ["pending"]
    assert token not in listed.text and "token" not in json.dumps(listed.json()["data"])
    (audit,) = audit_rows(auth_db, "invitation.created")
    assert audit[1:3] == ("user", world.user("owner@acme.test"))
    assert audit[7] == {"role": "reviewer"}


async def test_reinvite_revokes_the_first(api: httpx.AsyncClient, world: World) -> None:
    first = (await _invite(api, world, "again@acme.test")).json()
    second = (await _invite(api, world, "again@acme.test", role="operator")).json()
    listed = (await api.get(INVITES, headers=_as(world, "owner@acme.test"))).json()["data"]
    assert {i["id"]: i["status"] for i in listed} == {
        first["invite"]["id"]: "revoked",
        second["invite"]["id"]: "pending",
    }


async def test_email_is_trimmed_and_lowered(api: httpx.AsyncClient, world: World) -> None:
    first = await _invite(api, world, "A@B.co ")
    assert first.status_code == 201 and first.json()["invite"]["email"] == "a@b.co"
    await _invite(api, world, "a@b.co")
    listed = (await api.get(INVITES, headers=_as(world, "owner@acme.test"))).json()["data"]
    assert sorted(i["status"] for i in listed) == ["pending", "revoked"]


@pytest.mark.endpoint("invites_create", case="wrong_role")
@pytest.mark.parametrize("caller", ["reviewer@acme.test", "operator@acme.test"])
async def test_create_needs_team_manage(api: httpx.AsyncClient, world: World, caller: str) -> None:
    response = await _invite(api, world, "x@acme.test", caller=caller)
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "PERMISSION_DENIED"


async def test_admin_cannot_invite_an_owner(api: httpx.AsyncClient, world: World) -> None:
    response = await _invite(api, world, "boss@acme.test", role="owner", caller="admin@acme.test")
    assert response.status_code == 403
    owner = await _invite(api, world, "boss@acme.test", role="owner")
    assert owner.status_code == 201


@pytest.mark.endpoint("invites_create", case="validation")
async def test_create_member_email_is_422(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    active = await _invite(api, world, "Reviewer@Acme.test")
    assert active.status_code == 422
    assert active.json()["error"]["details"][0]["loc"] == ["body", "email"]
    auth_db.execute(
        "UPDATE tenant_memberships SET is_active = false, deactivated_at = now() "
        "WHERE user_id = %s",
        [world.user("operator@acme.test")],
    )
    assert (await _invite(api, world, "operator@acme.test")).status_code == 422
    # a member of another tenant is invitable
    assert (await _invite(api, world, "owner@bharat.test")).status_code == 201
    bad = await api.post(
        INVITES,
        json={"email": "x@acme.test", "role": "staff"},
        headers=_as(world, "owner@acme.test"),
    )
    assert bad.status_code == 422


async def test_51st_pending_invite_is_422(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    tenant, owner = world.tenant("acme"), world.user("owner@acme.test")
    for n in range(50):
        auth_db.execute(
            "INSERT INTO invitations (tenant_id, email, role, token_hash, invited_by_user_id, "
            "expires_at) VALUES (%s, %s, 'reviewer', %s, %s, now() + interval '7 days')",
            [tenant, f"p{n}@acme.test", hashlib.sha256(f"cap{n}".encode()).digest(), owner],
        )
    over = await _invite(api, world, "p50@acme.test")
    assert over.status_code == 422 and over.json()["error"]["details"][0]["loc"] == [
        "body",
        "email",
    ]
    again = await _invite(api, world, "p7@acme.test")
    assert again.status_code == 201


@pytest.mark.endpoint("invites_create", case="missing_auth")
@pytest.mark.endpoint("invites_list", case="missing_auth")
@pytest.mark.endpoint("invites_revoke", case="missing_auth")
async def test_invites_missing_auth_401(api: httpx.AsyncClient, world: World) -> None:
    assert (await api.post(INVITES, json={"email": "a@b.co", "role": "admin"})).status_code == 401
    assert (await api.get(INVITES)).status_code == 401
    assert (await api.post(f"{INVITES}/{uuid.uuid4()}/revoke")).status_code == 401


@pytest.mark.endpoint("invites_create", case="invalid_token", variant="expired")
@pytest.mark.endpoint("invites_list", case="invalid_token", variant="expired")
@pytest.mark.endpoint("invites_revoke", case="invalid_token", variant="expired")
@pytest.mark.endpoint("invites_join", case="invalid_token", variant="expired")
@pytest.mark.endpoint("members_password_reset", case="invalid_token", variant="expired")
async def test_invites_expired_token_401(api: httpx.AsyncClient, world: World) -> None:
    now = int(time.time())
    await _all_401(
        api, world, bearer(_token(world, "owner@acme.test", iat=now - 3600, exp=now - 1))
    )


@pytest.mark.endpoint("invites_create", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("invites_list", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("invites_revoke", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("invites_join", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("members_password_reset", case="invalid_token", variant="bad_signature")
async def test_invites_bad_signature_401(api: httpx.AsyncClient, world: World) -> None:
    await _all_401(api, world, bearer(_resigned(_token(world, "owner@acme.test"))))


@pytest.mark.endpoint("invites_create", case="invalid_token", variant="wrong_typ")
@pytest.mark.endpoint("invites_list", case="invalid_token", variant="wrong_typ")
@pytest.mark.endpoint("invites_revoke", case="invalid_token", variant="wrong_typ")
@pytest.mark.endpoint("invites_join", case="invalid_token", variant="wrong_typ")
@pytest.mark.endpoint("members_password_reset", case="invalid_token", variant="wrong_typ")
async def test_invites_refresh_token_401(api: httpx.AsyncClient, world: World) -> None:
    await _all_401(api, world, bearer(_token(world, "owner@acme.test", typ="refresh")))


async def _all_401(api: httpx.AsyncClient, world: World, headers: dict[str, str]) -> None:
    calls = [
        api.post(INVITES, json={"email": "a@b.co", "role": "admin"}, headers=headers),
        api.get(INVITES, headers=headers),
        api.post(f"{INVITES}/{uuid.uuid4()}/revoke", headers=headers),
        api.post(f"{INVITES}/join", json={"token": "T" * 43}, headers=headers),
        api.post(
            f"{PREFIX}/members/{world.user('reviewer@acme.test')}/password-reset", headers=headers
        ),
    ]
    for call in calls:
        response = await call
        assert response.status_code == 401, response.text


async def test_create_needs_step_up_under_mfa_all(
    api: httpx.AsyncClient, world: World, auth_db: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    from .mfa_support import mfa_on

    auth_db.execute(
        "INSERT INTO user_mfa_factors (user_id, kind, status, secret_blob, key_version, "
        "confirmed_at) VALUES (%s, 'totp', 'active', %s, 'local-v1', now())",
        [world.user("owner@acme.test"), b"x" * 40],
    )
    mfa_on(monkeypatch)
    response = await _invite(api, world, "late@acme.test")
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "MFA_STEP_UP_REQUIRED"


# ---- list (AC7) ----


@pytest.mark.endpoint("invites_list", case="success")
@pytest.mark.endpoint("invites_list", case="wrong_tenant")
@pytest.mark.endpoint("invites_create", case="wrong_tenant")
async def test_list_is_the_tenants_own(api: httpx.AsyncClient, world: World) -> None:
    mine = (await _invite(api, world, "mine@acme.test")).json()["invite"]["id"]
    theirs = await _invite(api, world, "theirs@bharat.test", caller="owner@bharat.test")
    assert theirs.status_code == 201
    acme = (await api.get(INVITES, headers=_as(world, "owner@acme.test"))).json()["data"]
    assert [i["id"] for i in acme] == [mine]
    bharat = (await api.get(INVITES, headers=_as(world, "owner@bharat.test"))).json()["data"]
    assert [i["id"] for i in bharat] == [theirs.json()["invite"]["id"]]


async def test_list_pages_and_filters(api: httpx.AsyncClient, world: World) -> None:
    ids = [(await _invite(api, world, f"pg{n}@acme.test")).json()["invite"]["id"] for n in range(3)]
    headers = _as(world, "owner@acme.test")
    first = (await api.get(INVITES, params={"limit": 2}, headers=headers)).json()
    assert first["page"]["has_more"] is True
    rest = await api.get(
        INVITES, params={"limit": 2, "cursor": first["page"]["next_cursor"]}, headers=headers
    )
    seen = [i["id"] for i in first["data"]] + [i["id"] for i in rest.json()["data"]]
    assert seen == list(reversed(ids))
    await api.post(f"{INVITES}/{ids[0]}/revoke", headers=headers)
    revoked = await api.get(INVITES, params={"status": "revoked"}, headers=headers)
    assert [i["id"] for i in revoked.json()["data"]] == [ids[0]]


@pytest.mark.endpoint("invites_list", case="validation")
async def test_list_bad_status_422(api: httpx.AsyncClient, world: World) -> None:
    response = await api.get(
        INVITES, params={"status": "open"}, headers=_as(world, "owner@acme.test")
    )
    assert response.status_code == 422


@pytest.mark.endpoint("invites_list", case="wrong_role")
async def test_list_needs_team_manage(api: httpx.AsyncClient, world: World) -> None:
    assert (await api.get(INVITES, headers=_as(world, "reviewer@acme.test"))).status_code == 403


# ---- revoke ----


@pytest.mark.endpoint("invites_revoke", case="success")
async def test_revoke_is_idempotent(api: httpx.AsyncClient, world: World, auth_db: Conn) -> None:
    invite = (await _invite(api, world, "gone@acme.test")).json()["invite"]["id"]
    headers = _as(world, "owner@acme.test")
    first = await api.post(f"{INVITES}/{invite}/revoke", headers=headers)
    assert first.status_code == 200 and first.json()["status"] == "revoked"
    again = await api.post(f"{INVITES}/{invite}/revoke", headers=headers)
    assert again.status_code == 200 and again.json() == first.json()
    assert len(audit_rows(auth_db, "invitation.revoked")) == 1


@pytest.mark.endpoint("invites_revoke", case="conflict")
async def test_revoke_accepted_is_409(api: httpx.AsyncClient, world: World, auth_db: Conn) -> None:
    invite = (await _invite(api, world, "done@acme.test")).json()["invite"]["id"]
    auth_db.execute(
        "UPDATE invitations SET accepted_at = now(), accepted_user_id = %s WHERE id = %s",
        [world.user("reviewer@acme.test"), invite],
    )
    response = await api.post(f"{INVITES}/{invite}/revoke", headers=_as(world, "owner@acme.test"))
    assert response.status_code == 409
    assert response.json()["error"]["type"] == "INVALID_STATUS_TRANSITION"


@pytest.mark.endpoint("invites_revoke", case="wrong_role")
async def test_revoke_owner_invite_needs_an_owner(api: httpx.AsyncClient, world: World) -> None:
    invite = (await _invite(api, world, "boss2@acme.test", role="owner")).json()["invite"]["id"]
    admin = await api.post(f"{INVITES}/{invite}/revoke", headers=_as(world, "admin@acme.test"))
    assert admin.status_code == 403
    reviewer = await api.post(
        f"{INVITES}/{invite}/revoke", headers=_as(world, "reviewer@acme.test")
    )
    assert reviewer.status_code == 403


@pytest.mark.endpoint("invites_revoke", case="wrong_tenant")
async def test_revoke_other_tenants_invite_404(api: httpx.AsyncClient, world: World) -> None:
    invite = (await _invite(api, world, "iso@acme.test")).json()["invite"]["id"]
    response = await api.post(f"{INVITES}/{invite}/revoke", headers=_as(world, "owner@bharat.test"))
    assert response.status_code == 404
    still = (await api.get(INVITES, headers=_as(world, "owner@acme.test"))).json()["data"]
    assert [i["status"] for i in still] == ["pending"]


@pytest.mark.endpoint("invites_revoke", case="not_found")
async def test_revoke_unknown_404(api: httpx.AsyncClient, world: World) -> None:
    response = await api.post(
        f"{INVITES}/{uuid.uuid4()}/revoke", headers=_as(world, "owner@acme.test")
    )
    assert response.status_code == 404


@pytest.mark.endpoint("invites_revoke", case="validation")
async def test_revoke_bad_id_422(api: httpx.AsyncClient, world: World) -> None:
    response = await api.post(f"{INVITES}/not-a-uuid/revoke", headers=_as(world, "owner@acme.test"))
    assert response.status_code == 422
