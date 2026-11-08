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


async def test_concurrent_invites_for_one_email_leave_one_pending(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    """A double-clicked Invite (tester R1): the per-email lock queues the creates, so every
    caller gets 201 and exactly one invite stays pending (no `uq_invitations_pending` 500)."""
    import asyncio

    responses = await asyncio.gather(*(_invite(api, world, "race@acme.test") for _ in range(4)))
    assert [r.status_code for r in responses] == [201] * 4, [r.text for r in responses]
    rows = auth_db.execute(
        "SELECT revoked_at IS NULL FROM invitations WHERE email = 'race@acme.test'"
    ).fetchall()
    assert sorted(rows) == [(False,)] * 3 + [(True,)]


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


# ---- part 2: lookup, accept with a password, join (AC3, AC5, AC6) ----

NEW_PASSWORD = "Brass-Lantern-Path-0123"
OTHER_PASSWORD = NEW_PASSWORD.replace("Brass", "Other")
# DPP rules: a password holding the email's local part (`weak`) is refused
PERSONAL_PASSWORD = NEW_PASSWORD.replace("Brass", "Weak")


async def _token_for(api: httpx.AsyncClient, world: World, email: str, **kwargs: Any) -> str:
    response = await _invite(api, world, email, **kwargs)
    assert response.status_code == 201, response.text
    return _fragment(response.json()["invite_url"])


async def _accept(
    api: httpx.AsyncClient, token: str, password: str = NEW_PASSWORD, **extra: Any
) -> httpx.Response:
    return await api.post(f"{INVITES}/accept", json={"token": token, "password": password, **extra})


def _body_without_correlation(response: httpx.Response) -> dict[str, Any]:
    body = response.json()
    body["error"].pop("correlation_id", None)
    report = body["error"].get("error_report")
    if report:
        report.pop("request_id", None)
    return body  # type: ignore[no-any-return]


@pytest.mark.endpoint("invites_lookup", case="success")
async def test_lookup_previews_the_invite(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    token = await _token_for(api, world, "new@acme.test", role="operator")
    response = await api.post(f"{INVITES}/lookup", json={"token": token})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tenant_name"] == "Acme" and body["role"] == "operator"
    assert body["email"] == "new@acme.test" and body["account"] == "new"
    existing = await _token_for(api, world, "owner@bharat.test")
    preview = await api.post(f"{INVITES}/lookup", json={"token": existing})
    assert preview.json()["account"] == "existing"


@pytest.mark.endpoint("invites_lookup", case="validation")
@pytest.mark.endpoint("invites_accept", case="validation")
@pytest.mark.endpoint("invites_join", case="validation")
@pytest.mark.parametrize("token", ["T" * 42, "T" * 42 + "="])
async def test_malformed_token_is_422(api: httpx.AsyncClient, world: World, token: str) -> None:
    assert (await api.post(f"{INVITES}/lookup", json={"token": token})).status_code == 422
    assert (await _accept(api, token)).status_code == 422
    joined = await api.post(
        f"{INVITES}/join", json={"token": token}, headers=_as(world, "owner@bharat.test")
    )
    assert joined.status_code == 422


@pytest.mark.endpoint("invites_accept", case="success")
async def test_accept_creates_the_account_and_membership(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    from .mfa_support import login

    token = await _token_for(api, world, "meera@acme.test")
    response = await _accept(api, token, display_name="Meera Iyer")
    assert response.status_code == 200, response.text
    assert response.json() == {"email": "meera@acme.test", "tenant_name": "Acme"}
    row = auth_db.execute(
        "SELECT m.tenant_id, m.role, m.invited_by_user_id, u.display_name, i.accepted_user_id "
        "FROM users u JOIN tenant_memberships m ON m.user_id = u.id "
        "JOIN invitations i ON i.accepted_user_id = u.id WHERE u.email = 'meera@acme.test'"
    ).fetchall()
    assert len(row) == 1
    tenant, role, inviter, name, accepted = row[0]
    assert (tenant, role, inviter, name) == (
        world.tenant("acme"),
        "reviewer",
        world.user("owner@acme.test"),
        "Meera Iyer",
    )
    (audit,) = audit_rows(auth_db, "invitation.accepted")
    assert audit[1:3] == ("user", accepted) and audit[7] == {"role": "reviewer"}
    signed = await login(api, "meera@acme.test", NEW_PASSWORD)
    assert signed.status_code == 200 and signed.json()["status"] == "signed_in"


async def test_accepted_member_enrols_under_mfa_all(
    api: httpx.AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from .mfa_support import login, mfa_on

    token = await _token_for(api, world, "mfa@acme.test")
    assert (await _accept(api, token)).status_code == 200
    mfa_on(monkeypatch)
    signed = await login(api, "mfa@acme.test", NEW_PASSWORD)
    assert signed.json()["status"] == "mfa_enrollment_required"


async def test_accept_weak_password_writes_nothing(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    token = await _token_for(api, world, "weak@acme.test")
    short = await _accept(api, token, password="short")
    assert short.status_code == 422
    assert short.json()["error"]["details"][0]["loc"] == ["body", "password"]
    personal = await _accept(api, token, password=PERSONAL_PASSWORD)
    assert personal.status_code == 422  # holds the email's local part
    assert auth_db.execute("SELECT 1 FROM users WHERE email = 'weak@acme.test'").fetchone() is None
    assert (await _accept(api, token)).status_code == 200


@pytest.mark.endpoint("invites_accept", case="conflict")
async def test_accept_for_an_existing_account_is_409(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    token = await _token_for(api, world, "owner@bharat.test")
    response = await _accept(api, token)
    assert response.status_code == 409
    assert response.json()["error"]["type"] == "INVALID_STATUS_TRANSITION"
    assert auth_db.execute(
        "SELECT count(*) FROM tenant_memberships WHERE user_id = %s",
        [world.user("owner@bharat.test")],
    ).fetchone() == (1,)


APPLICANT = "founder@kalakar.test"
APPLICANT_PASSWORD = "Quiet-Harbour-Lamp-7305"


def _members(conn: Conn, email: str) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT m.tenant_id, m.role, m.is_active FROM tenant_memberships m "
        "JOIN users u ON u.id = m.user_id WHERE u.email = %s ORDER BY m.created_at",
        [email],
    ).fetchall()


async def test_a_pending_applicant_joins_with_its_own_password(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    """CR1: a self-serve applicant has a users row and no membership, so the login is 401 and
    `invites_join` is out of reach. Lookup says `own_password`, and accept takes the account's
    own password under the lockout gate: a wrong one is 401 and counted, the right one joins."""
    from .mfa_support import login

    signed_up = await api.post(
        f"{PREFIX}/signup",
        json={
            "brand_name": "Kalakar Weaves",
            "owner_name": "Meera Iyer",
            "email": APPLICANT,
            "password": APPLICANT_PASSWORD,
        },
    )
    assert signed_up.status_code == 202, signed_up.text
    assert (await login(api, APPLICANT, APPLICANT_PASSWORD)).status_code == 401
    token = await _token_for(api, world, APPLICANT)
    preview = await api.post(f"{INVITES}/lookup", json={"token": token})
    assert preview.status_code == 200 and preview.json()["account"] == "own_password"
    wrong = await _accept(api, token, password=NEW_PASSWORD)
    assert wrong.status_code == 401, wrong.text
    assert wrong.json()["error"]["type"] == "UNAUTHENTICATED"
    assert auth_db.execute(
        "SELECT failed_auth_count FROM users WHERE email = %s", [APPLICANT]
    ).fetchone() == (1,)
    assert _members(auth_db, APPLICANT) == []
    done = await _accept(api, token, password=APPLICANT_PASSWORD, display_name="Someone Else")
    assert done.status_code == 200, done.text
    assert done.json() == {"email": APPLICANT, "tenant_name": "Acme"}
    assert auth_db.execute(
        "SELECT count(*), max(display_name) FROM users WHERE email = %s", [APPLICANT]
    ).fetchone() == (1, "Meera Iyer")
    assert _members(auth_db, APPLICANT) == [(world.tenant("acme"), "reviewer", True)]
    signed = await login(api, APPLICANT, APPLICANT_PASSWORD)
    assert signed.status_code == 200 and signed.json()["status"] == "signed_in", signed.text


@pytest.mark.parametrize("lost", ["deactivated", "suspended", "offboarded"])
async def test_an_account_with_no_live_brand_joins_with_its_password(
    api: httpx.AsyncClient, world: World, auth_db: Conn, lost: str
) -> None:
    """CR1: deactivated from every brand, or the only brand suspended or offboarded: the login
    is refused (401; 403 `TENANT_SUSPENDED` for a suspended brand since SI10), so lookup says
    `own_password` and accept with the account's own password joins."""
    from .auth_support import PASSWORD, add_member, add_user
    from .mfa_support import login

    email = f"{lost}@elsewhere.test"
    bharat = world.tenant("bharat")
    add_member(auth_db, bharat, add_user(auth_db, email), "reviewer", active=lost != "deactivated")
    if lost != "deactivated":
        auth_db.execute(
            "UPDATE tenants SET status = %s, offboarded_at = CASE WHEN %s = 'offboarded' "
            "THEN now() END WHERE id = %s",
            [lost, lost, bharat],
        )
    assert (await login(api, email, PASSWORD)).status_code == (403 if lost == "suspended" else 401)
    token = await _token_for(api, world, email)
    preview = await api.post(f"{INVITES}/lookup", json={"token": token})
    assert preview.json()["account"] == "own_password", preview.text
    done = await _accept(api, token, password=PASSWORD)
    assert done.status_code == 200, done.text
    assert (world.tenant("acme"), "reviewer", True) in _members(auth_db, email)
    assert (await login(api, email, PASSWORD)).status_code == 200


async def test_a_google_only_account_that_cannot_sign_in_stays_new(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    """CR2: `own_password` needs a password to verify. A Google-only account deactivated from
    every brand has none, so lookup stays `new` (KNOWN-ISSUES: no way in yet)."""
    from .auth_support import add_member, add_user

    email = "google-only@elsewhere.test"
    user_id = add_user(auth_db, email)
    auth_db.execute(
        "UPDATE users SET hashed_password = NULL, auth_provider = 'google', "
        "google_sub = 'g-sub-stranded', email_verified_at = now() WHERE id = %s",
        [user_id],
    )
    add_member(auth_db, world.tenant("bharat"), user_id, "reviewer", active=False)
    token = await _token_for(api, world, email)
    preview = await api.post(f"{INVITES}/lookup", json={"token": token})
    assert preview.status_code == 200 and preview.json()["account"] == "new", preview.text


async def test_accept_into_a_brand_it_already_belongs_to_is_409(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    """CR1 edge: a membership here that appeared after the invite (inactive, so the login is
    401) is the join's 409, never the membership key's 500."""
    from .auth_support import PASSWORD, add_member, add_user

    token = await _token_for(api, world, "late@acme.test")
    add_member(
        auth_db, world.tenant("acme"), add_user(auth_db, "late@acme.test"), "reviewer", active=False
    )
    response = await _accept(api, token, password=PASSWORD)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["type"] == "INVALID_STATUS_TRANSITION"


def _second_owner(conn: Conn, world: World) -> None:
    """Another active owner, so the last-owner guard lets the inviter go."""
    from .auth_support import add_member, add_user

    add_member(conn, world.tenant("acme"), add_user(conn, "owner2@acme.test"), "owner")


def _demote_inviter(conn: Conn, world: World) -> None:
    _second_owner(conn, world)
    conn.execute(
        "UPDATE tenant_memberships SET role = 'admin' WHERE user_id = %s",
        [world.user("owner@acme.test")],
    )


@pytest.mark.parametrize(
    "spoil",
    [
        "unknown",
        "replayed",
        "revoked",
        "expired",
        "suspended",
        "inviter_deactivated",
        "owner_invite_inviter_demoted",
    ],
)
async def test_every_refusal_is_one_404(
    api: httpx.AsyncClient, world: World, auth_db: Conn, spoil: str
) -> None:
    role = "owner" if spoil == "owner_invite_inviter_demoted" else "reviewer"
    token = await _token_for(api, world, f"{spoil}@acme.test", role=role)
    invite = auth_db.execute(
        "SELECT id FROM invitations WHERE email = %s", [f"{spoil}@acme.test"]
    ).fetchone()
    assert invite is not None
    if spoil == "unknown":
        token = "U" * 43
    elif spoil == "replayed":
        assert (await _accept(api, token)).status_code == 200
    elif spoil == "revoked":
        await api.post(f"{INVITES}/{invite[0]}/revoke", headers=_as(world, "owner@acme.test"))
    elif spoil == "expired":
        auth_db.execute("UPDATE invitations SET expires_at = now() WHERE id = %s", [invite[0]])
    elif spoil == "suspended":
        auth_db.execute(
            "UPDATE tenants SET status = 'suspended' WHERE id = %s", [world.tenant("acme")]
        )
    elif spoil == "inviter_deactivated":
        _second_owner(auth_db, world)
        auth_db.execute(
            "UPDATE tenant_memberships SET is_active = false, deactivated_at = now() "
            "WHERE user_id = %s",
            [world.user("owner@acme.test")],
        )
    else:
        _demote_inviter(auth_db, world)
    looked = await api.post(f"{INVITES}/lookup", json={"token": token})
    accepted = await _accept(api, token, password=OTHER_PASSWORD)
    reference = await api.post(f"{INVITES}/lookup", json={"token": "Z" * 43})
    for response in (looked, accepted, reference):
        assert response.status_code == 404, (spoil, response.text)
    assert _body_without_correlation(looked) == _body_without_correlation(reference)
    assert _body_without_correlation(accepted) == _body_without_correlation(reference)
    assert reference.json()["error"]["type"] == "NOT_FOUND"


async def test_two_concurrent_accepts_make_one_member(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    import asyncio

    token = await _token_for(api, world, "race@acme.test")
    first, second = await asyncio.gather(
        _accept(api, token), _accept(api, token, password=OTHER_PASSWORD)
    )
    assert sorted([first.status_code, second.status_code]) == [200, 404]
    assert auth_db.execute(
        "SELECT count(*) FROM users WHERE email = 'race@acme.test'"
    ).fetchone() == (1,)
    assert auth_db.execute(
        "SELECT count(*) FROM tenant_memberships m JOIN users u ON u.id = m.user_id "
        "WHERE u.email = 'race@acme.test'"
    ).fetchone() == (1,)


@pytest.mark.endpoint("invites_join", case="success")
@pytest.mark.endpoint("invites_join", case="wrong_tenant")
async def test_join_with_the_matching_account(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    token = await _token_for(api, world, "owner@bharat.test", role="admin")
    caller = _as(world, "owner@bharat.test")
    joined = await api.post(f"{INVITES}/join", json={"token": token}, headers=caller)
    assert joined.status_code == 200, joined.text
    assert joined.json() == {"tenant_id": str(world.tenant("acme")), "role": "admin"}
    rows = auth_db.execute(
        "SELECT tenant_id, role FROM tenant_memberships WHERE user_id = %s ORDER BY role",
        [world.user("owner@bharat.test")],
    ).fetchall()
    assert set(rows) == {(world.tenant("acme"), "admin"), (world.tenant("bharat"), "owner")}
    switched = await api.post(
        f"{PREFIX}/auth/switch-tenant",
        json={"target_tenant_id": str(world.tenant("acme"))},
        headers=caller,
    )
    assert switched.status_code == 200, switched.text
    again = await api.post(f"{INVITES}/join", json={"token": token}, headers=caller)
    assert again.status_code == 404  # used


async def test_join_with_another_email_is_404(api: httpx.AsyncClient, world: World) -> None:
    token = await _token_for(api, world, "owner@bharat.test")
    other = await api.post(
        f"{INVITES}/join", json={"token": token}, headers=_as(world, "admin@bharat.test")
    )
    assert other.status_code == 404
    unknown = await api.post(
        f"{INVITES}/join", json={"token": "Z" * 43}, headers=_as(world, "admin@bharat.test")
    )
    assert _body_without_correlation(other) == _body_without_correlation(unknown)


@pytest.mark.endpoint("invites_join", case="conflict")
async def test_join_when_already_a_member_is_409(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    token = await _token_for(api, world, "owner@bharat.test")
    from .auth_support import add_member

    add_member(
        auth_db, world.tenant("acme"), world.user("owner@bharat.test"), "operator", active=False
    )
    response = await api.post(
        f"{INVITES}/join", json={"token": token}, headers=_as(world, "owner@bharat.test")
    )
    assert response.status_code == 409


@pytest.mark.endpoint("invites_join", case="missing_auth")
async def test_join_missing_auth_401(api: httpx.AsyncClient) -> None:
    assert (await api.post(f"{INVITES}/join", json={"token": "T" * 43})).status_code == 401


async def test_accept_long_display_name_is_422(api: httpx.AsyncClient, world: World) -> None:
    token = await _token_for(api, world, "long@acme.test")
    assert (await _accept(api, token, display_name="x" * 200)).status_code == 422


# ---- V1 hardening #12: approvers come only from owners, and a copied link verifies nothing ----


@pytest.mark.parametrize("role", ["reviewer", "owner"])
async def test_admin_cannot_invite_an_approver(
    api: httpx.AsyncClient, world: World, role: str
) -> None:
    response = await _invite(api, world, f"{role}-x@acme.test", role=role, caller="admin@acme.test")
    assert response.status_code == 403, response.text
    allowed = await _invite(api, world, "op-x@acme.test", role="operator", caller="admin@acme.test")
    assert allowed.status_code == 201, allowed.text


async def test_admin_cannot_promote_a_member_to_reviewer(
    api: httpx.AsyncClient, world: World
) -> None:
    target = world.user("operator@acme.test")
    url = f"{PREFIX}/members/{target}"
    denied = await api.patch(url, json={"role": "reviewer"}, headers=_as(world, "admin@acme.test"))
    assert denied.status_code == 403, denied.text
    done = await api.patch(url, json={"role": "reviewer"}, headers=_as(world, "owner@acme.test"))
    assert done.status_code == 200, done.text


async def test_password_accept_account_is_unverified_and_not_an_approver(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    from app.repositories.uow import tenant_uow

    token = await _token_for(api, world, "copied@acme.test")  # a reviewer invite, by the owner
    assert (await _accept(api, token)).status_code == 200
    (row,) = auth_db.execute(
        "SELECT id, email_unverified FROM users WHERE email = 'copied@acme.test'"
    ).fetchall()
    user_id, unverified = row
    assert unverified is True
    acme = world.tenant("acme")
    async with tenant_uow(acme) as uow:
        assert await uow.members.approver_state(user_id) == ("reviewer", False)
        reviewer = world.user("reviewer@acme.test")
        assert await uow.members.approver_state(reviewer) == ("reviewer", True)
        approvers = await uow.members.approver_ids(frozenset({"reviewer", "owner"}))
    assert user_id not in approvers and reviewer in approvers
