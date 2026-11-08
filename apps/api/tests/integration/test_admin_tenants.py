"""SI5 T9 (AC11, ADR-0029 §5): the platform admin's tenant list, activate and extend-trial through
the real app as app_rw. The admin is `operator@acme.test` with `is_platform_admin` (the sign-up
suite's pattern); every move is audited as staff with the admin's email."""

from __future__ import annotations

import time
import uuid
from typing import Any

import httpx
import jwt
import pytest
from tests.conftest import CI_JWT_SECRET

from .auth_support import PREFIX, Conn, World, audit_rows, bearer, build_world, tokens_for
from .conftest import LOCAL_HOSTS
from .mfa_support import login

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

TENANTS = f"{PREFIX}/admin/tenants"
ADMIN = "operator@acme.test"
REASON = {"reason": "Paid by bank transfer."}


@pytest.fixture
def world(auth_db: Conn) -> World:
    built = build_world(auth_db)
    auth_db.execute("UPDATE users SET is_platform_admin = true WHERE email = %s", [ADMIN])
    return built


def _as(world: World, email: str = ADMIN) -> dict[str, str]:
    slug = email.split("@", 1)[1].split(".", 1)[0]
    return bearer(tokens_for(world, email, slug))


def _expire(conn: Conn, tenant: uuid.UUID) -> None:
    conn.execute(
        "UPDATE tenants SET status = 'trial', trial_ends_at = now() - interval '1 hour' "
        "WHERE id = %s",
        [tenant],
    )
    conn.execute("SET SESSION AUTHORIZATION migrator")
    try:
        conn.execute("SELECT expire_trials()")
    finally:
        conn.execute("RESET SESSION AUTHORIZATION")
    assert conn.execute("SELECT status FROM tenants WHERE id = %s", [tenant]).fetchone() == (
        "suspended",
    )


def _status(conn: Conn, tenant: uuid.UUID) -> str:
    row = conn.execute("SELECT status FROM tenants WHERE id = %s", [tenant]).fetchone()
    assert row is not None
    return str(row[0])


async def _activate(api: httpx.AsyncClient, world: World, tenant: Any) -> httpx.Response:
    return await api.post(f"{TENANTS}/{tenant}/activate", json=REASON, headers=_as(world))


async def _extend(
    api: httpx.AsyncClient, world: World, tenant: Any, days: Any = 30
) -> httpx.Response:
    return await api.post(
        f"{TENANTS}/{tenant}/extend-trial",
        json={"days": days, "reason": "Pilot runs longer."},
        headers=_as(world),
    )


# ---- activate (AC11) ----


@pytest.mark.endpoint("admin_tenants_activate", case="success")
@pytest.mark.endpoint("admin_tenants_activate", case="wrong_tenant")
async def test_activate_a_trial_expired_tenant(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    bharat = world.tenant("bharat")
    _expire(auth_db, bharat)
    refused = await login(api, "owner@bharat.test")
    # SI10: a verified password whose only brand is suspended is told so
    assert refused.status_code == 403
    assert refused.json()["error"]["type"] == "TENANT_SUSPENDED"
    response = await _activate(api, world, bharat)
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["id"], body["slug"], body["status"]) == (str(bharat), "bharat", "active")
    (audit,) = audit_rows(auth_db, "tenant.active")
    assert (audit[0], audit[1], audit[2], audit[3]) == (bharat, "staff", None, ADMIN)
    assert audit[9] == "Paid by bank transfer."
    from tests.support.rate_limits import reset_rate_limits

    reset_rate_limits(auth_db)
    assert (await login(api, "owner@bharat.test")).status_code == 200


async def test_activate_active_is_the_same_row(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    first = await _activate(api, world, world.tenant("bharat"))
    assert first.status_code == 200 and first.json()["status"] == "active"
    assert audit_rows(auth_db, "tenant.active") == []


@pytest.mark.endpoint("admin_tenants_activate", case="conflict")
async def test_activate_offboarded_is_409(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    bharat = world.tenant("bharat")
    auth_db.execute(
        "UPDATE tenants SET status = 'offboarded', offboarded_at = now(), "
        "purge_after = now() + interval '30 days' WHERE id = %s",
        [bharat],
    )
    response = await _activate(api, world, bharat)
    assert response.status_code == 409
    assert response.json()["error"]["type"] == "INVALID_STATUS_TRANSITION"
    assert _status(auth_db, bharat) == "offboarded"


async def test_activate_unknown_is_404(api: httpx.AsyncClient, world: World) -> None:
    assert (await _activate(api, world, uuid.uuid4())).status_code == 404


@pytest.mark.endpoint("admin_tenants_activate", case="wrong_role")
@pytest.mark.endpoint("admin_tenants_extend_trial", case="wrong_role")
@pytest.mark.endpoint("admin_tenants_list", case="wrong_role")
async def test_non_admin_is_403(api: httpx.AsyncClient, world: World, auth_db: Conn) -> None:
    bharat = world.tenant("bharat")
    _expire(auth_db, bharat)
    owner = _as(world, "owner@acme.test")  # an owner of another, running tenant
    calls = [
        api.post(f"{TENANTS}/{bharat}/activate", json=REASON, headers=owner),
        api.post(
            f"{TENANTS}/{bharat}/extend-trial", json={"days": 5, "reason": "r"}, headers=owner
        ),
        api.get(TENANTS, headers=owner),
    ]
    for call in calls:
        response = await call
        assert response.status_code == 403, response.text
    assert _status(auth_db, bharat) == "suspended"


@pytest.mark.endpoint("admin_tenants_activate", case="validation")
@pytest.mark.endpoint("admin_tenants_extend_trial", case="validation")
async def test_bad_bodies_are_422(api: httpx.AsyncClient, world: World) -> None:
    bharat = world.tenant("bharat")
    headers = _as(world)
    for body in ({}, {"reason": ""}, {"reason": "x" * 501}):
        response = await api.post(f"{TENANTS}/{bharat}/activate", json=body, headers=headers)
        assert response.status_code == 422, body
    for days in (0, 91, 1.5, True, "7"):
        assert (await _extend(api, world, bharat, days)).status_code == 422, days
    assert (await _activate(api, world, "not-a-uuid")).status_code == 422


# ---- extend-trial (AC11) ----


@pytest.mark.endpoint("admin_tenants_extend_trial", case="success")
@pytest.mark.endpoint("admin_tenants_extend_trial", case="wrong_tenant")
async def test_extend_a_trial_expired_tenant(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    bharat = world.tenant("bharat")
    _expire(auth_db, bharat)
    response = await _extend(api, world, bharat, 30)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "trial"
    assert auth_db.execute(
        "SELECT abs(extract(epoch FROM trial_ends_at - (now() + interval '30 days'))) < 60 "
        "FROM tenants WHERE id = %s",
        [bharat],
    ).fetchone() == (True,)
    (audit,) = audit_rows(auth_db, "tenant.trial_extended")
    assert (audit[1], audit[3], audit[9]) == ("staff", ADMIN, "Pilot runs longer.")


async def test_extend_a_running_trial_adds_to_its_end(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    bharat = world.tenant("bharat")
    auth_db.execute(
        "UPDATE tenants SET status = 'trial', trial_ends_at = now() + interval '10 days' "
        "WHERE id = %s",
        [bharat],
    )
    assert (await _extend(api, world, bharat, 5)).status_code == 200
    assert auth_db.execute(
        "SELECT abs(extract(epoch FROM trial_ends_at - (now() + interval '15 days'))) < 60 "
        "FROM tenants WHERE id = %s",
        [bharat],
    ).fetchone() == (True,)


@pytest.mark.endpoint("admin_tenants_extend_trial", case="conflict")
async def test_extend_refusals_are_409(api: httpx.AsyncClient, world: World, auth_db: Conn) -> None:
    bharat = world.tenant("bharat")
    assert (await _extend(api, world, bharat)).status_code == 409  # active
    auth_db.execute("SET SESSION AUTHORIZATION migrator")
    try:
        auth_db.execute("SELECT set_tenant_lifecycle(%s, 'suspended', 'unpaid')", [bharat])
    finally:
        auth_db.execute("RESET SESSION AUTHORIZATION")
    assert (await _extend(api, world, bharat)).status_code == 409  # suspended by staff
    assert (await _extend(api, world, uuid.uuid4())).status_code == 404


# ---- list ----


@pytest.mark.endpoint("admin_tenants_list", case="success")
@pytest.mark.endpoint("admin_tenants_list", case="wrong_tenant")
async def test_list_every_tenant_with_paging_and_a_filter(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    from .auth_support import add_tenant

    extra = add_tenant(auth_db, "kalakar", status="trial")
    first = (await api.get(TENANTS, params={"limit": 2}, headers=_as(world))).json()
    assert first["page"]["has_more"] is True
    rest = (
        await api.get(
            TENANTS,
            params={"limit": 2, "cursor": first["page"]["next_cursor"]},
            headers=_as(world),
        )
    ).json()
    seen = [t["id"] for t in first["data"] + rest["data"]]
    assert set(seen) == {str(world.tenant("acme")), str(world.tenant("bharat")), str(extra)}
    assert len(seen) == 3
    trial = (await api.get(TENANTS, params={"status": "trial"}, headers=_as(world))).json()
    assert [t["id"] for t in trial["data"]] == [str(extra)]
    assert trial["data"][0]["trial_ends_at"] is not None


@pytest.mark.endpoint("admin_tenants_list", case="validation")
async def test_list_bad_status_is_422(api: httpx.AsyncClient, world: World) -> None:
    response = await api.get(TENANTS, params={"status": "open"}, headers=_as(world))
    assert response.status_code == 422


@pytest.mark.endpoint("admin_tenants_list", case="missing_auth")
@pytest.mark.endpoint("admin_tenants_activate", case="missing_auth")
@pytest.mark.endpoint("admin_tenants_extend_trial", case="missing_auth")
async def test_missing_auth_401(api: httpx.AsyncClient, world: World) -> None:
    assert await _all(api, {}) == [401, 401, 401]


def _claims(world: World, **overrides: Any) -> str:
    now = int(time.time())
    claims = {
        "sub": str(world.user(ADMIN)), "tid": str(world.tenant("acme")), "roles": ["operator"],
        "email": ADMIN, "typ": "access", "jti": str(uuid.uuid4()),
        "iss": "http://localhost:8000", "aud": "ai-marketplace-os", "iat": now, "exp": now + 600,
        **overrides,
    }  # fmt: skip
    return jwt.encode(claims, CI_JWT_SECRET, algorithm="HS256")


async def _all(api: httpx.AsyncClient, headers: dict[str, str]) -> list[int]:
    tid = str(uuid.uuid4())
    return [
        (await api.get(TENANTS, headers=headers)).status_code,
        (await api.post(f"{TENANTS}/{tid}/activate", json=REASON, headers=headers)).status_code,
        (
            await api.post(
                f"{TENANTS}/{tid}/extend-trial", json={"days": 5, "reason": "r"}, headers=headers
            )
        ).status_code,
    ]


@pytest.mark.endpoint("admin_tenants_list", case="invalid_token", variant="expired")
@pytest.mark.endpoint("admin_tenants_activate", case="invalid_token", variant="expired")
@pytest.mark.endpoint("admin_tenants_extend_trial", case="invalid_token", variant="expired")
async def test_expired_token_401(api: httpx.AsyncClient, world: World) -> None:
    now = int(time.time())
    assert await _all(api, bearer(_claims(world, iat=now - 3600, exp=now - 1))) == [401] * 3


@pytest.mark.endpoint("admin_tenants_list", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("admin_tenants_activate", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("admin_tenants_extend_trial", case="invalid_token", variant="bad_signature")
async def test_bad_signature_401(api: httpx.AsyncClient, world: World) -> None:
    forged = jwt.encode(
        jwt.decode(_claims(world), options={"verify_signature": False}),
        "another_secret_that_is_at_least_32_chars",
        algorithm="HS256",
    )
    assert await _all(api, bearer(forged)) == [401] * 3


@pytest.mark.endpoint("admin_tenants_list", case="invalid_token", variant="wrong_typ")
@pytest.mark.endpoint("admin_tenants_activate", case="invalid_token", variant="wrong_typ")
@pytest.mark.endpoint("admin_tenants_extend_trial", case="invalid_token", variant="wrong_typ")
async def test_refresh_token_401(api: httpx.AsyncClient, world: World) -> None:
    assert await _all(api, bearer(_claims(world, typ="refresh"))) == [401] * 3
