"""ADR-0032: an owner adds another brand without a new sign-up review, through the real app as
app_rw: the new trial tenant and owner membership, the audit rows, the website on the brand
profile, the switch into it and its isolation, the trial clock, the refusals (an admin, a suspended owned
brand, the trial and owned-brand caps, an applicant with no brand), the switch, the step-up, the limiter and the idempotency key.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import httpx
import jwt
import pytest
from tests.conftest import CI_JWT_SECRET

from .auth_support import (
    PREFIX,
    Conn,
    World,
    add_member,
    add_tenant,
    add_user,
    audit_rows,
    bearer,
    build_world,
    tokens_for,
)
from .conftest import LOCAL_HOSTS

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

TENANTS = f"{PREFIX}/tenants"
BODY = {"name": "Kalakar Home", "website": "https://kalakar.example"}


@pytest.fixture
def world(auth_db: Conn, monkeypatch: pytest.MonkeyPatch) -> World:
    _switch(monkeypatch, on=True)
    return build_world(auth_db)


def _switch(monkeypatch: pytest.MonkeyPatch, *, on: bool, cap: int | None = None) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("TENANT_CREATE_ENABLED", "true" if on else "false")
    if cap is not None:
        monkeypatch.setenv("TENANT_CREATE_MAX_OWNED", str(cap))
    get_settings.cache_clear()


def _as(world: World, email: str) -> dict[str, str]:
    slug = email.split("@", 1)[1].split(".", 1)[0]
    return bearer(tokens_for(world, email, slug))


def _key() -> dict[str, str]:
    return {"Idempotency-Key": f"brand-{uuid.uuid4()}"}


async def _create(
    api: httpx.AsyncClient,
    world: World,
    caller: str = "owner@acme.test",
    body: dict[str, Any] | None = None,
    key: dict[str, str] | None = None,
) -> httpx.Response:
    headers = {**_as(world, caller), **(key or _key())}
    return await api.post(TENANTS, json=BODY if body is None else body, headers=headers)


def _token(world: World, email: str, **overrides: Any) -> str:
    now = int(time.time())
    claims = {
        "sub": str(world.user(email)),
        "tid": str(world.tenant("acme")),
        "roles": ["owner"],
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


def _tenant_row(conn: Conn, tenant_id: str) -> tuple[Any, ...]:
    row = conn.execute(
        "SELECT name, slug, status, trial_ends_at > now() + interval '29 days' FROM tenants "
        "WHERE id = %s",
        [tenant_id],
    ).fetchone()
    assert row is not None
    return tuple(row)


def _tenant_count(conn: Conn) -> int:
    row = conn.execute("SELECT count(*) FROM tenants").fetchone()
    assert row is not None
    return int(row[0])


# ---- success: the tenant, the owner, the audit, the profile ----


@pytest.mark.endpoint("tenants_create", case="success")
async def test_owner_adds_a_trial_brand_they_own(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    owner = world.user("owner@acme.test")
    response = await _create(api, world)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "Kalakar Home" and body["slug"] == "kalakar-home"
    assert body["status"] == "trial" and body["role"] == "owner"
    assert body["trial_ends_at"] is not None
    new_id = body["id"]
    assert _tenant_row(auth_db, new_id) == ("Kalakar Home", "kalakar-home", "trial", True)
    members = auth_db.execute(
        "SELECT user_id, role, is_active FROM tenant_memberships WHERE tenant_id = %s", [new_id]
    ).fetchall()
    assert members == [(owner, "owner", True)]
    site = auth_db.execute(
        "SELECT website_url FROM brand_profiles WHERE tenant_id = %s", [new_id]
    ).fetchone()
    assert site == ("https://kalakar.example",)
    for action in ("tenant.created", "membership.created", "tenant.added_by_owner"):
        (row,) = audit_rows(auth_db, action)
        assert (str(row[0]), row[1], row[2], row[3]) == (new_id, "user", owner, None), action
    (added,) = audit_rows(auth_db, "tenant.added_by_owner")
    assert added[4:6] == ("tenant", uuid.UUID(new_id)) and added[7] == {"name": "Kalakar Home"}
    me = (await api.get(f"{PREFIX}/auth/me", headers=_as(world, "owner@acme.test"))).json()
    assert {m["tenant_id"]: m["role"] for m in me["memberships"]}[new_id] == "owner"


async def test_no_website_makes_no_profile_row(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    response = await _create(api, world, body={"name": "Plain Brand"})
    assert response.status_code == 201, response.text
    rows = auth_db.execute(
        "SELECT 1 FROM brand_profiles WHERE tenant_id = %s", [response.json()["id"]]
    ).fetchall()
    assert rows == []


async def test_a_taken_slug_gets_a_suffix(api: httpx.AsyncClient, world: World) -> None:
    response = await _create(api, world, body={"name": "Acme"})
    assert response.status_code == 201, response.text
    assert response.json()["slug"].startswith("acme-") and response.json()["name"] == "Acme"


async def test_switch_into_the_new_brand_sees_only_its_own_data(
    api: httpx.AsyncClient, world: World
) -> None:
    new_id = (await _create(api, world)).json()["id"]
    switched = await api.post(
        f"{PREFIX}/auth/switch-tenant",
        json={"target_tenant_id": new_id},
        headers=_as(world, "owner@acme.test"),
    )
    assert switched.status_code == 200, switched.text
    headers = bearer(switched.json()["access_token"])
    me = (await api.get(f"{PREFIX}/tenants/me", headers=headers)).json()
    assert me["id"] == new_id and me["status"] == "trial"
    members = await api.get(f"{PREFIX}/members", headers=headers)
    assert members.status_code == 200, members.text
    assert [m["user_id"] for m in members.json()["data"]] == [str(world.user("owner@acme.test"))]
    products = await api.get(f"{PREFIX}/products", headers=headers)
    assert products.status_code == 200 and products.json()["data"] == []
    # nobody else in acme joins the new brand
    other = (await api.get(f"{PREFIX}/auth/me", headers=_as(world, "admin@acme.test"))).json()
    assert new_id not in {m["tenant_id"] for m in other["memberships"]}


@pytest.mark.endpoint("tenants_create", case="wrong_tenant")
async def test_the_new_brand_belongs_to_its_creator_only(
    api: httpx.AsyncClient, world: World
) -> None:
    theirs = await _create(api, world, caller="owner@bharat.test", body={"name": "Bharat Two"})
    assert theirs.status_code == 201
    acme = (await api.get(f"{PREFIX}/auth/me", headers=_as(world, "owner@acme.test"))).json()
    assert theirs.json()["id"] not in {m["tenant_id"] for m in acme["memberships"]}
    switched = await api.post(
        f"{PREFIX}/auth/switch-tenant",
        json={"target_tenant_id": theirs.json()["id"]},
        headers=_as(world, "owner@acme.test"),
    )
    assert switched.status_code == 404


# ---- refusals ----


@pytest.mark.endpoint("tenants_create", case="wrong_role")
@pytest.mark.parametrize("caller", ["operator@acme.test", "reviewer@acme.test"])
async def test_create_needs_settings_manage(
    api: httpx.AsyncClient, world: World, auth_db: Conn, caller: str
) -> None:
    before = _tenant_count(auth_db)
    response = await _create(api, world, caller=caller)
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "PERMISSION_DENIED"
    assert _tenant_count(auth_db) == before


async def test_an_admin_is_not_an_owner(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    before = _tenant_count(auth_db)
    response = await _create(api, world, caller="admin@acme.test")
    assert response.status_code == 403
    error = response.json()["error"]
    assert error["type"] == "PERMISSION_DENIED" and "owner" in error["message"]
    assert _tenant_count(auth_db) == before
    assert audit_rows(auth_db, "tenant.added_by_owner") == []


async def test_an_owner_with_a_suspended_brand_is_refused(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    held = add_tenant(auth_db, "held", status="suspended")
    add_member(auth_db, held, world.user("owner@acme.test"), "owner")
    before = _tenant_count(auth_db)
    response = await _create(api, world)
    assert response.status_code == 403
    assert "suspended" in response.json()["error"]["message"]
    assert _tenant_count(auth_db) == before


async def test_a_suspended_brand_they_do_not_own_does_not_refuse(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    client = add_tenant(auth_db, "client", status="suspended")
    add_member(auth_db, client, world.user("owner@acme.test"), "operator")
    response = await _create(api, world)
    assert response.status_code == 201, response.text


def _trial_owner(world: World, conn: Conn) -> str:
    tenant = add_tenant(conn, "trialco", status="trial")
    email = "owner@trialco.test"
    world.tenants["trialco"] = tenant
    world.users[email] = add_user(conn, email)
    add_member(conn, tenant, world.users[email], "owner")
    return email


async def test_a_brand_added_on_trial_keeps_the_owners_trial_end(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    caller = _trial_owner(world, auth_db)
    response = await _create(api, world, caller=caller)
    assert response.status_code == 201, response.text
    rows = auth_db.execute(
        "SELECT t.trial_ends_at = own.trial_ends_at, t.status FROM tenants t, tenants own "
        "WHERE t.id = %s AND own.id = %s",
        [response.json()["id"], world.tenant("trialco")],
    ).fetchone()
    assert rows == (True, "trial")


async def test_an_owner_on_trial_owns_at_most_three(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    from tests.support.rate_limits import reset_rate_limits

    caller = _trial_owner(world, auth_db)
    made = [
        (await _create(api, world, caller=caller, body={"name": f"Trial {n}"})).status_code
        for n in range(2)
    ]
    assert made == [201, 201]
    reset_rate_limits(auth_db)
    before = _tenant_count(auth_db)
    over = await _create(api, world, caller=caller, body={"name": "Trial 3"})
    assert over.status_code == 403
    assert "On a trial" in over.json()["error"]["message"]
    assert _tenant_count(auth_db) == before


async def test_the_owned_brand_cap(
    api: httpx.AsyncClient, world: World, auth_db: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.support.rate_limits import reset_rate_limits

    _switch(monkeypatch, on=True, cap=3)
    first = await _create(api, world, body={"name": "Two"})
    second = await _create(api, world, body={"name": "Three"})
    assert (first.status_code, second.status_code) == (201, 201)
    reset_rate_limits(auth_db)
    over = await _create(api, world, body={"name": "Four"})
    assert over.status_code == 403
    assert "most brands" in over.json()["error"]["message"]
    # a brand the owner is only an operator in does not count
    other = add_tenant(auth_db, "other")
    add_member(auth_db, other, world.user("owner@bharat.test"), "operator")
    assert (await _create(api, world, caller="owner@bharat.test")).status_code == 201


async def test_an_applicant_with_no_brand_has_no_way_in(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    applicant = add_user(auth_db, "applicant@new.test")
    auth_db.execute(
        "INSERT INTO signup_requests (user_id, brand_name, owner_name) "
        "VALUES (%s, 'New Brand', 'Asha')",
        [applicant],
    )
    world.users["applicant@new.test"] = applicant
    response = await api.post(
        TENANTS, json=BODY, headers={**bearer(_token(world, "applicant@new.test")), **_key()}
    )
    assert response.status_code == 401
    assert audit_rows(auth_db, "tenant.added_by_owner") == []


async def test_switched_off_is_501(
    api: httpx.AsyncClient, world: World, auth_db: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switch(monkeypatch, on=False)
    before = _tenant_count(auth_db)
    response = await _create(api, world)
    assert response.status_code == 501
    assert response.json()["error"]["type"] == "NOT_IMPLEMENTED"
    assert _tenant_count(auth_db) == before
    caps = (await api.get(f"{PREFIX}/tenants/me", headers=_as(world, "owner@acme.test"))).json()
    assert caps["capabilities"]["tenant_create_enabled"] is False


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
    response = await _create(api, world)
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "MFA_STEP_UP_REQUIRED"
    fresh = bearer(
        tokens_for(world, "owner@acme.test", "acme", amr=["pwd", "otp"], mfa_at=int(time.time()))
    )
    stepped = await api.post(TENANTS, json=BODY, headers={**fresh, **_key()})
    assert stepped.status_code == 201, stepped.text


async def test_create_is_rate_limited_per_user(
    api: httpx.AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import get_settings

    _switch(monkeypatch, on=True, cap=50)
    monkeypatch.setenv("AUTH_LOGIN_RATE_LIMIT_PER_MIN", "2")
    get_settings.cache_clear()
    statuses = [
        (await _create(api, world, body={"name": f"Brand {n}"})).status_code for n in range(3)
    ]
    assert statuses == [201, 201, 429]


# ---- idempotency ----


@pytest.mark.endpoint("tenants_create", case="conflict")
async def test_same_key_replays_and_another_body_is_409(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    key = _key()
    first = await _create(api, world, key=key)
    again = await _create(api, world, key=key)
    assert (first.status_code, again.status_code) == (201, 201)
    assert again.json() == first.json()
    assert len(audit_rows(auth_db, "tenant.added_by_owner")) == 1
    other = await _create(api, world, body={"name": "Another"}, key=key)
    assert other.status_code == 409
    assert other.json()["error"]["type"] == "IDEMPOTENCY_KEY_REUSED"


# ---- validation and auth ----


@pytest.mark.endpoint("tenants_create", case="validation")
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"name": "  "},
        {"name": "x" * 201},
        {"name": "Ok", "website": "not a url"},
        {"name": "Ok", "tenant_id": str(uuid.uuid4())},
        {"name": "Ok", "status": "active"},
    ],
)
async def test_bad_body_is_422(
    api: httpx.AsyncClient, world: World, auth_db: Conn, body: dict[str, Any]
) -> None:
    before = _tenant_count(auth_db)
    response = await _create(api, world, body=body)
    assert response.status_code == 422, response.text
    assert _tenant_count(auth_db) == before


async def test_missing_idempotency_key_is_422(api: httpx.AsyncClient, world: World) -> None:
    response = await api.post(TENANTS, json=BODY, headers=_as(world, "owner@acme.test"))
    assert response.status_code == 422


@pytest.mark.endpoint("tenants_create", case="missing_auth")
async def test_missing_auth_401(api: httpx.AsyncClient, world: World) -> None:
    assert (await api.post(TENANTS, json=BODY, headers=_key())).status_code == 401


@pytest.mark.endpoint("tenants_create", case="invalid_token", variant="expired")
async def test_expired_token_401(api: httpx.AsyncClient, world: World) -> None:
    now = int(time.time())
    token = _token(world, "owner@acme.test", iat=now - 3600, exp=now - 1)
    response = await api.post(TENANTS, json=BODY, headers={**bearer(token), **_key()})
    assert response.status_code == 401


@pytest.mark.endpoint("tenants_create", case="invalid_token", variant="bad_signature")
async def test_bad_signature_401(api: httpx.AsyncClient, world: World) -> None:
    claims = jwt.decode(_token(world, "owner@acme.test"), options={"verify_signature": False})
    token = jwt.encode(claims, "another_secret_that_is_at_least_32_chars", algorithm="HS256")
    response = await api.post(TENANTS, json=BODY, headers={**bearer(token), **_key()})
    assert response.status_code == 401


@pytest.mark.endpoint("tenants_create", case="invalid_token", variant="wrong_typ")
async def test_refresh_token_401(api: httpx.AsyncClient, world: World) -> None:
    token = _token(world, "owner@acme.test", typ="refresh")
    response = await api.post(TENANTS, json=BODY, headers={**bearer(token), **_key()})
    assert response.status_code == 401
