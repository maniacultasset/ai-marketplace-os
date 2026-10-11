"""ADR-0019: self-serve sign-up with platform-admin approval, through the real app as app_rw
under FORCE RLS (POST /signup, POST /signup/status, the admin list, approve and reject, and the
`user make-admin` CLI).

The world is S10's two tenants with one user per role; `operator@acme.test` is made the platform
admin (an operator, so the role in the token's tenant is shown not to matter) and every other
member is not one.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import httpx
import jwt
import pytest
from tests.conftest import CI_JWT_SECRET

from .auth_support import PREFIX, Conn, World, audit_rows, bearer, build_world, tokens_for
from .cli_support import cli
from .conftest import LOCAL_HOSTS

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

SIGNUP = f"{PREFIX}/signup"
STATUS = f"{PREFIX}/signup/status"
QUEUE = f"{PREFIX}/admin/signup-requests"
ADMIN = "operator@acme.test"
PASSWORD = "kalakar-pass-0123"
FORM: dict[str, Any] = {
    "brand_name": "Kalakar Weaves",
    "owner_name": "Meera Iyer",
    "email": "founder@kalakar.test",
    "password": PASSWORD,
}


@pytest.fixture
def world(auth_db: Conn) -> World:
    world = build_world(auth_db)
    auth_db.execute("UPDATE users SET is_platform_admin = true WHERE email = %s", [ADMIN])
    return world


def _as(world: World, email: str) -> dict[str, str]:
    slug = email.split("@", 1)[1].split(".", 1)[0]
    return bearer(tokens_for(world, email, slug))


async def _signup(api: httpx.AsyncClient, **changes: Any) -> httpx.Response:
    return await api.post(SIGNUP, json={**FORM, **changes})


async def _pending(api: httpx.AsyncClient, **changes: Any) -> str:
    response = await _signup(api, **changes)
    assert response.status_code == 202, response.text
    return str(response.json()["request_id"])


async def _approve(
    api: httpx.AsyncClient, world: World, request_id: str, body: Any = None, *, who: str = ADMIN
) -> httpx.Response:
    kwargs: dict[str, Any] = {"headers": _as(world, who)}
    if body is not None:
        kwargs["json"] = body
    return await api.post(f"{QUEUE}/{request_id}/approve", **kwargs)


async def _reject(
    api: httpx.AsyncClient, world: World, request_id: str, reason: str = "Not verified.",
    *, who: str = ADMIN,
) -> httpx.Response:  # fmt: skip
    return await api.post(
        f"{QUEUE}/{request_id}/reject", json={"reason": reason}, headers=_as(world, who)
    )


async def _login(api: httpx.AsyncClient, email: str = FORM["email"]) -> httpx.Response:
    return await api.post(f"{PREFIX}/auth/login", json={"email": email, "password": PASSWORD})


def _request_row(conn: Conn, request_id: str) -> tuple[Any, ...] | None:
    return conn.execute(
        "SELECT status, reason, created_tenant_id, decided_by_user_id, decided_at IS NOT NULL "
        "FROM signup_requests WHERE id = %s",
        [request_id],
    ).fetchone()


def _count(conn: Conn, sql: str, params: list[Any] | None = None) -> int:
    row = conn.execute(sql, params or []).fetchone()
    assert row is not None
    return int(row[0])


# ---- POST /signup ----


@pytest.mark.endpoint("signup_create", case="success")
async def test_signup_creates_user_and_pending_request(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    response = await _signup(api, phone="+91 98765 43210", website="https://kalakar.test")
    assert response.status_code == 202, response.text
    body = response.json()
    assert set(body) == {"request_id", "status"} and body["status"] == "pending"
    user = auth_db.execute(
        "SELECT id, display_name, is_platform_admin, hashed_password FROM users WHERE email = %s",
        [FORM["email"]],
    ).fetchone()
    assert user is not None
    assert user[1] == "Meera Iyer" and user[2] is False
    assert user[3].startswith("$argon2id$") and PASSWORD not in user[3]
    row = auth_db.execute(
        "SELECT user_id, brand_name, owner_name, phone, website, status FROM signup_requests "
        "WHERE id = %s",
        [body["request_id"]],
    ).fetchone()
    assert row == (
        user[0], "Kalakar Weaves", "Meera Iyer", "+91 98765 43210", "https://kalakar.test",
        "pending",
    )  # fmt: skip
    [audit] = audit_rows(auth_db, "signup.requested")
    assert audit[0] is None and audit[1] == "system" and audit[5] == user[0]
    # no tenant, no membership for a pending applicant
    assert (
        _count(auth_db, "SELECT count(*) FROM tenant_memberships WHERE user_id = %s", [user[0]])
        == 0
    )


@pytest.mark.endpoint("signup_create", case="validation")
@pytest.mark.parametrize(
    "changes",
    [
        {"password": "short"},
        {"email": "not-an-email"},
        {"brand_name": "   "},
        {"website": "javascript:alert(1)"},
        {"is_platform_admin": True},
        {"status": "approved"},
    ],
)
async def test_signup_validation_422_writes_nothing(
    api: httpx.AsyncClient, world: World, auth_db: Conn, changes: dict[str, Any]
) -> None:
    response = await _signup(api, **changes)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["type"] == "VALIDATION_FAILED"
    assert _count(auth_db, "SELECT count(*) FROM signup_requests") == 0
    assert _count(auth_db, "SELECT count(*) FROM users WHERE email = %s", [FORM["email"]]) == 0


async def test_signup_mass_assignment_cannot_make_an_admin(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    assert (await _signup(api, is_platform_admin=True)).status_code == 422
    assert (await _signup(api)).status_code == 202
    flag = auth_db.execute(
        "SELECT is_platform_admin FROM users WHERE email = %s", [FORM["email"]]
    ).fetchone()
    assert flag == (False,)


async def test_signup_twice_while_pending_answers_the_same_request(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    first = await _pending(api)
    second = await _pending(api, brand_name="Another Brand")
    assert second == first
    assert _count(auth_db, "SELECT count(*) FROM signup_requests") == 1
    assert len(audit_rows(auth_db, "signup.requested")) == 1


async def test_signup_existing_email_wrong_password_reveals_nothing(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    response = await _signup(api, email="owner@acme.test", password="not-the-password-0")
    assert response.status_code == 202
    assert set(response.json()) == {"request_id", "status"}
    assert _count(auth_db, "SELECT count(*) FROM signup_requests") == 0
    assert audit_rows(auth_db, "signup.requested") == []


async def test_signup_existing_member_right_password_opens_a_request(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    from .auth_support import PASSWORD as WORLD_PASSWORD

    response = await _signup(
        api, email="owner@acme.test", password=WORLD_PASSWORD, brand_name="Acme Two"
    )
    assert response.status_code == 202
    row = auth_db.execute(
        "SELECT user_id FROM signup_requests WHERE id = %s", [response.json()["request_id"]]
    ).fetchone()
    assert row == (world.user("owner@acme.test"),)


async def test_signup_ip_rate_limited(api: httpx.AsyncClient, world: World) -> None:
    statuses = [(await _signup(api, email=f"brand{i}@kalakar.test")).status_code for i in range(6)]
    assert statuses[:5] == [202] * 5
    assert statuses[5] == 429
    limited = await _signup(api, email="brand9@kalakar.test")
    assert limited.status_code == 429 and int(limited.headers["retry-after"]) >= 1
    assert limited.json()["error"]["type"] == "RATE_LIMITED"


# ---- pending: no login, no tenant, status readable ----


@pytest.mark.endpoint("signup_status", case="success")
async def test_pending_applicant_cannot_log_in_but_reads_status(
    api: httpx.AsyncClient, world: World
) -> None:
    request_id = await _pending(api)
    assert (await _login(api)).status_code == 401
    response = await api.post(STATUS, json={"email": FORM["email"], "password": PASSWORD})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["request_id"] == request_id and body["status"] == "pending"
    assert body["reason"] is None and body["tenant_id"] is None and body["decided_at"] is None


@pytest.mark.endpoint("signup_status", case="validation")
@pytest.mark.parametrize(
    "body", [{}, {"email": "x"}, {"email": "a@b.test", "password": ""}, {"password": "x"}]
)
async def test_signup_status_validation_422(
    api: httpx.AsyncClient, world: World, body: dict[str, Any]
) -> None:
    assert (await api.post(STATUS, json=body)).status_code == 422


@pytest.mark.parametrize(
    "creds",
    [
        {"email": FORM["email"], "password": "wrong-password-00"},
        {"email": "nobody@kalakar.test", "password": PASSWORD},
        {"email": "owner@acme.test", "password": "test-password-0123"},  # a member, no request
    ],
)
async def test_signup_status_refusals_are_one_401(
    api: httpx.AsyncClient, world: World, creds: dict[str, str]
) -> None:
    await _pending(api)
    response = await api.post(STATUS, json=creds)
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "UNAUTHENTICATED"


async def test_pending_applicant_reaches_no_tenant_data(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    """Even a correctly signed token naming a real tenant fails the membership probe."""
    await _pending(api)
    user_id = auth_db.execute("SELECT id FROM users WHERE email = %s", [FORM["email"]]).fetchone()[
        0
    ]  # type: ignore[index]
    now = int(time.time())
    forged = jwt.encode(
        {
            "sub": str(user_id), "tid": str(world.tenant("acme")), "roles": ["owner"],
            "email": FORM["email"], "typ": "access", "jti": str(uuid.uuid4()),
            "iss": "http://localhost:8000", "aud": "ai-marketplace-os", "iat": now,
            "exp": now + 600,
        },
        CI_JWT_SECRET,
        algorithm="HS256",
    )  # fmt: skip
    for path in ("/tenants/me", "/products", "/members", "/auth/me", "/admin/signup-requests"):
        response = await api.get(f"{PREFIX}{path}", headers=bearer(forged))
        assert response.status_code == 401, path


# ---- GET /admin/signup-requests ----


@pytest.mark.endpoint("admin_signup_requests_list", case="success")
@pytest.mark.endpoint("admin_signup_requests_list", case="wrong_tenant")
async def test_admin_lists_every_request_whatever_its_tenant(
    api: httpx.AsyncClient, world: World
) -> None:
    first = await _pending(api)
    second = await _pending(api, email="second@other.test", brand_name="Other")
    for who in (ADMIN,):  # an operator of acme: the role and the tenant do not narrow the queue
        response = await api.get(QUEUE, params={"status": "pending"}, headers=_as(world, who))
        assert response.status_code == 200, response.text
        body = response.json()
        assert [r["id"] for r in body["data"]] == [first, second]
        assert body["data"][0]["email"] == FORM["email"]
        assert body["data"][0]["owner_name"] == "Meera Iyer"
        assert body["page"] == {"next_cursor": None, "has_more": False, "limit": 50}


async def test_admin_list_pages_and_filters(api: httpx.AsyncClient, world: World) -> None:
    ids = [await _pending(api, email=f"b{i}@kalakar.test") for i in range(3)]
    await _reject(api, world, ids[0])
    first = (await api.get(QUEUE, params={"limit": 1}, headers=_as(world, ADMIN))).json()
    assert [r["id"] for r in first["data"]] == ids[:1] and first["page"]["has_more"] is True
    rest = (
        await api.get(
            QUEUE,
            params={"limit": 5, "cursor": first["page"]["next_cursor"]},
            headers=_as(world, ADMIN),
        )
    ).json()
    assert [r["id"] for r in rest["data"]] == ids[1:]
    pending = (await api.get(QUEUE, params={"status": "pending"}, headers=_as(world, ADMIN))).json()
    assert [r["id"] for r in pending["data"]] == ids[1:]
    rejected = (
        await api.get(QUEUE, params={"status": "rejected"}, headers=_as(world, ADMIN))
    ).json()
    assert [r["id"] for r in rejected["data"]] == ids[:1]


@pytest.mark.endpoint("admin_signup_requests_list", case="validation")
@pytest.mark.parametrize("params", [{"limit": "0"}, {"cursor": "!!"}, {"status": "maybe"}])
async def test_admin_list_validation_422(
    api: httpx.AsyncClient, world: World, params: dict[str, str]
) -> None:
    assert (await api.get(QUEUE, params=params, headers=_as(world, ADMIN))).status_code == 422


NON_ADMINS = ["owner@acme.test", "admin@acme.test", "owner@bharat.test", "reviewer@bharat.test"]


@pytest.mark.endpoint("admin_signup_requests_list", case="wrong_role")
@pytest.mark.endpoint("admin_signup_requests_approve", case="wrong_role")
@pytest.mark.endpoint("admin_signup_requests_reject", case="wrong_role")
@pytest.mark.parametrize("who", NON_ADMINS)
async def test_non_admin_cannot_list_or_decide_403(
    api: httpx.AsyncClient, world: World, auth_db: Conn, who: str
) -> None:
    request_id = await _pending(api)
    responses = [
        await api.get(QUEUE, headers=_as(world, who)),
        await _approve(api, world, request_id, who=who),
        await _reject(api, world, request_id, who=who),
    ]
    for response in responses:
        assert response.status_code == 403, response.text
        assert response.json()["error"]["type"] == "PERMISSION_DENIED"
    assert _request_row(auth_db, request_id) == ("pending", None, None, None, False)
    assert _count(auth_db, "SELECT count(*) FROM tenants") == 2


async def test_revoked_admin_flag_takes_effect_at_once(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    headers = _as(world, ADMIN)
    assert (await api.get(QUEUE, headers=headers)).status_code == 200
    auth_db.execute("UPDATE users SET is_platform_admin = false WHERE email = %s", [ADMIN])
    assert (await api.get(QUEUE, headers=headers)).status_code == 403


@pytest.mark.endpoint("admin_signup_requests_list", case="missing_auth")
@pytest.mark.endpoint("admin_signup_requests_approve", case="missing_auth")
@pytest.mark.endpoint("admin_signup_requests_reject", case="missing_auth")
async def test_admin_ops_missing_auth_401(api: httpx.AsyncClient, world: World) -> None:
    rid = str(uuid.uuid4())
    assert (await api.get(QUEUE)).status_code == 401
    assert (await api.post(f"{QUEUE}/{rid}/approve")).status_code == 401
    assert (await api.post(f"{QUEUE}/{rid}/reject", json={"reason": "x"})).status_code == 401


def _claims(world: World, **overrides: Any) -> str:
    now = int(time.time())
    claims = {
        "sub": str(world.user(ADMIN)), "tid": str(world.tenant("acme")), "roles": ["operator"],
        "email": ADMIN, "typ": "access", "jti": str(uuid.uuid4()),
        "iss": "http://localhost:8000", "aud": "ai-marketplace-os", "iat": now, "exp": now + 600,
        **overrides,
    }  # fmt: skip
    return jwt.encode(claims, CI_JWT_SECRET, algorithm="HS256")


async def _all_admin_ops(api: httpx.AsyncClient, headers: dict[str, str]) -> list[int]:
    rid = str(uuid.uuid4())
    return [
        (await api.get(QUEUE, headers=headers)).status_code,
        (await api.post(f"{QUEUE}/{rid}/approve", headers=headers)).status_code,
        (
            await api.post(f"{QUEUE}/{rid}/reject", json={"reason": "x"}, headers=headers)
        ).status_code,
    ]


@pytest.mark.endpoint("admin_signup_requests_list", case="invalid_token", variant="expired")
@pytest.mark.endpoint("admin_signup_requests_approve", case="invalid_token", variant="expired")
@pytest.mark.endpoint("admin_signup_requests_reject", case="invalid_token", variant="expired")
async def test_admin_ops_expired_token_401(api: httpx.AsyncClient, world: World) -> None:
    now = int(time.time())
    expired = bearer(_claims(world, iat=now - 3600, exp=now - 1))
    assert await _all_admin_ops(api, expired) == [401, 401, 401]


@pytest.mark.endpoint("admin_signup_requests_list", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint(
    "admin_signup_requests_approve", case="invalid_token", variant="bad_signature"
)
@pytest.mark.endpoint("admin_signup_requests_reject", case="invalid_token", variant="bad_signature")
async def test_admin_ops_bad_signature_401(api: httpx.AsyncClient, world: World) -> None:
    forged = jwt.encode(
        jwt.decode(_claims(world), options={"verify_signature": False}),
        "another_secret_that_is_at_least_32_chars",
        algorithm="HS256",
    )
    assert await _all_admin_ops(api, bearer(forged)) == [401, 401, 401]


@pytest.mark.endpoint("admin_signup_requests_list", case="invalid_token", variant="wrong_typ")
@pytest.mark.endpoint("admin_signup_requests_approve", case="invalid_token", variant="wrong_typ")
@pytest.mark.endpoint("admin_signup_requests_reject", case="invalid_token", variant="wrong_typ")
async def test_admin_ops_refresh_token_401(api: httpx.AsyncClient, world: World) -> None:
    assert await _all_admin_ops(api, bearer(_claims(world, typ="refresh"))) == [401, 401, 401]


# ---- POST /admin/signup-requests/{request_id}/approve ----


@pytest.mark.endpoint("admin_signup_requests_approve", case="success")
async def test_approve_creates_trial_tenant_owner_and_audit(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    request_id = await _pending(api)
    response = await _approve(api, world, request_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "approved" and body["decided_at"] is not None
    tenant_id = body["tenant_id"]
    tenant = auth_db.execute(
        "SELECT name, slug, status, trial_ends_at > now() + interval '13 days' FROM tenants "
        "WHERE id = %s",
        [tenant_id],
    ).fetchone()
    assert tenant == ("Kalakar Weaves", "kalakar-weaves", "trial", True)
    applicant = auth_db.execute(
        "SELECT id FROM users WHERE email = %s", [FORM["email"]]
    ).fetchone()[
        0
    ]  # type: ignore[index]
    member = auth_db.execute(
        "SELECT role, is_active FROM tenant_memberships WHERE tenant_id = %s AND user_id = %s",
        [tenant_id, applicant],
    ).fetchone()
    assert member == ("owner", True)
    row = _request_row(auth_db, request_id)
    assert row == ("approved", None, uuid.UUID(tenant_id), world.user(ADMIN), True)
    for action in ("tenant.created", "membership.created", "signup.approved"):
        [audit] = audit_rows(auth_db, action)
        assert str(audit[0]) == tenant_id and audit[1] == "staff" and audit[3] == ADMIN, action


async def test_approved_owner_logs_in_and_onboards(api: httpx.AsyncClient, world: World) -> None:
    request_id = await _pending(api)
    assert (await _approve(api, world, request_id, {"trial_days": 30})).status_code == 200
    login = await _login(api)
    assert login.status_code == 200, login.text
    tokens = login.json()
    assert [m["tenant_name"] for m in tokens["memberships"]] == ["Kalakar Weaves"]
    headers = bearer(tokens["access_token"])
    me = (await api.get(f"{PREFIX}/auth/me", headers=headers)).json()
    assert me["role"] == "owner" and me["is_platform_admin"] is False
    profile = await api.get(f"{PREFIX}/tenants/me", headers=headers)
    assert profile.status_code == 200 and profile.json()["status"] == "trial"
    patched = await api.patch(
        f"{PREFIX}/tenants/me",
        json={"tone": "Warm, handloom-first."},
        headers={**headers, "If-Match": profile.headers["etag"]},
    )
    assert patched.status_code == 200, patched.text
    status = (await api.post(STATUS, json={"email": FORM["email"], "password": PASSWORD})).json()
    assert status["status"] == "approved" and status["tenant_id"] == profile.json()["id"]


@pytest.mark.endpoint("admin_signup_requests_approve", case="wrong_tenant")
@pytest.mark.endpoint("admin_signup_requests_reject", case="wrong_tenant")
async def test_admin_tenant_does_not_leak_into_the_new_tenant(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    """The admin acts from acme; the approved brand's tenant is its own, acme members see
    nothing of it, and a rejection touches no tenant at all."""
    approved = await _pending(api)
    rejected = await _pending(api, email="second@other.test", brand_name="Other")
    body = (await _approve(api, world, approved)).json()
    assert body["tenant_id"] not in {str(t) for t in world.tenants.values()}
    assert (await _reject(api, world, rejected)).status_code == 200
    members = (await api.get(f"{PREFIX}/members", headers=_as(world, "owner@acme.test"))).json()
    assert FORM["email"] not in {m["email"] for m in members["data"]}
    assert (
        _count(
            auth_db,
            "SELECT count(*) FROM tenant_memberships WHERE tenant_id = %s",
            [world.tenant("acme")],
        )
        == 4
    )


async def test_approve_twice_is_idempotent(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    request_id = await _pending(api)
    first = await _approve(api, world, request_id)
    second = await _approve(api, world, request_id, {"trial_days": 90})
    assert first.status_code == second.status_code == 200
    assert second.json() == first.json()
    assert _count(auth_db, "SELECT count(*) FROM tenants") == 3
    assert len(audit_rows(auth_db, "signup.approved")) == 1


async def test_approve_takes_a_free_slug(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    request_id = await _pending(api, brand_name="ACME")
    tenant_id = (await _approve(api, world, request_id)).json()["tenant_id"]
    slug = auth_db.execute("SELECT slug FROM tenants WHERE id = %s", [tenant_id]).fetchone()[0]  # type: ignore[index]
    assert slug.startswith("acme-") and len(slug) == len("acme-") + 4


@pytest.mark.endpoint("admin_signup_requests_approve", case="conflict")
async def test_approve_rejected_request_409(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    request_id = await _pending(api)
    assert (await _reject(api, world, request_id)).status_code == 200
    response = await _approve(api, world, request_id)
    assert response.status_code == 409
    assert response.json()["error"]["type"] == "INVALID_STATUS_TRANSITION"
    assert _count(auth_db, "SELECT count(*) FROM tenants") == 2


@pytest.mark.endpoint("admin_signup_requests_approve", case="validation")
@pytest.mark.parametrize(
    ("path_id", "body"),
    [("not-a-uuid", None), (None, {"trial_days": 0}), (None, {"tenant_id": "x"})],
)
async def test_approve_validation_422(
    api: httpx.AsyncClient, world: World, path_id: str | None, body: Any
) -> None:
    request_id = path_id or await _pending(api)
    assert (await _approve(api, world, request_id, body)).status_code == 422


async def test_decisions_unknown_id_404(api: httpx.AsyncClient, world: World) -> None:
    rid = str(uuid.uuid4())
    assert (await _approve(api, world, rid)).status_code == 404
    assert (await _reject(api, world, rid)).status_code == 404


# ---- POST /admin/signup-requests/{request_id}/reject ----


@pytest.mark.endpoint("admin_signup_requests_reject", case="success")
async def test_reject_stores_reason_and_the_applicant_reads_it(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    request_id = await _pending(api)
    reason = "We could not verify the brand; write to support with your GSTIN."
    response = await _reject(api, world, request_id, reason)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "rejected" and response.json()["reason"] == reason
    assert _request_row(auth_db, request_id) == ("rejected", reason, None, world.user(ADMIN), True)
    [audit] = audit_rows(auth_db, "signup.rejected")
    assert audit[0] is None and audit[1] == "staff" and audit[3] == ADMIN and audit[9] == reason
    assert (await _login(api)).status_code == 401
    status = (await api.post(STATUS, json={"email": FORM["email"], "password": PASSWORD})).json()
    assert status["status"] == "rejected" and status["reason"] == reason
    # the brand may apply again: a new pending request
    again = await _pending(api)
    assert again != request_id
    assert (await _reject(api, world, request_id, "other")).json()["reason"] == reason


@pytest.mark.endpoint("admin_signup_requests_reject", case="conflict")
async def test_reject_approved_request_409(api: httpx.AsyncClient, world: World) -> None:
    request_id = await _pending(api)
    assert (await _approve(api, world, request_id)).status_code == 200
    response = await _reject(api, world, request_id)
    assert response.status_code == 409
    assert response.json()["error"]["type"] == "INVALID_STATUS_TRANSITION"


@pytest.mark.endpoint("admin_signup_requests_reject", case="validation")
@pytest.mark.parametrize("body", [{}, {"reason": ""}, {"reason": "x" * 1001}])
async def test_reject_validation_422(
    api: httpx.AsyncClient, world: World, body: dict[str, Any]
) -> None:
    request_id = await _pending(api)
    response = await api.post(f"{QUEUE}/{request_id}/reject", json=body, headers=_as(world, ADMIN))
    assert response.status_code == 422


# ---- /auth/me and the CLI ----


async def test_me_exposes_the_platform_admin_flag(api: httpx.AsyncClient, world: World) -> None:
    admin = (await api.get(f"{PREFIX}/auth/me", headers=_as(world, ADMIN))).json()
    owner = (await api.get(f"{PREFIX}/auth/me", headers=_as(world, "owner@acme.test"))).json()
    assert admin["is_platform_admin"] is True and owner["is_platform_admin"] is False


async def test_cli_make_admin(
    api: httpx.AsyncClient, world: World, auth_db: Conn, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["user", "make-admin", "--email", "Owner@Bharat.test", "--staff-email", "s@ops.test"]
    code, out, err = await cli(argv, capsys)
    assert (code, err) == (0, "")
    assert out.strip().splitlines()[-1].endswith("platform_admin=true")
    assert (await api.get(QUEUE, headers=_as(world, "owner@bharat.test"))).status_code == 200
    again, _, _ = await cli(argv, capsys)
    assert again == 0
    [audit] = audit_rows(auth_db, "user.platform_admin_granted")
    assert audit[0] is None and audit[1] == "staff" and audit[3] == "s@ops.test"
    unknown, _, err = await cli(
        ["user", "make-admin", "--email", "nobody@x.test", "--staff-email", "s@ops.test"], capsys
    )
    assert unknown == 4 and "refused" in err
