"""S26 T3 (AC 2, OWASP API2): broken authentication.

Every non-public op refuses every malformed bearer with 401 `UNAUTHENTICATED` in the envelope;
refresh-token reuse revokes the family; logout ends the refresh; the sixth login in a minute is
429; an unknown email and a wrong password look the same; a deactivated member's live token is
refused; a tenant switch needs an active membership, and the token issued before a switch stays
scoped to the old tenant.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import httpx
import jwt
import pytest
from app.api.v1.surface import Operation
from tests.conftest import CI_JWT_SECRET
from tests.integration.auth_support import PASSWORD, add_member

from .conftest import World2, ids_of, ops, send

NON_PUBLIC = ops(public=False)
VARIANTS = ("expired", "forged_sig", "wrong_typ", "alg_none", "no_tid", "empty", "basic_scheme")
LOGIN = "/api/v1/auth/login"
REFRESH = "/api/v1/auth/refresh"
LOGOUT = "/api/v1/auth/logout"
SWITCH = "/api/v1/auth/switch-tenant"


def _claims(*, typ: str = "access") -> dict[str, Any]:
    """Well-formed claims of an owner (no database row needed: every variant is refused
    before the membership probe)."""
    from app.core.security import issue_claims

    return issue_claims(
        sub=uuid.uuid4(),
        tid=uuid.uuid4(),
        role="owner",
        email="owner@acme.test",
        typ=typ,  # type: ignore[arg-type]
    ).model_dump(mode="json")


def bad_header(variant: str) -> dict[str, str]:
    claims = _claims()
    if variant == "expired":
        now = int(time.time())
        claims.update(iat=now - 3600, exp=now - 1)
        token = jwt.encode(claims, CI_JWT_SECRET, algorithm="HS256")
    elif variant == "forged_sig":
        token = jwt.encode(claims, "s26_not_the_ci_secret_at_least_32_chars!", algorithm="HS256")
    elif variant == "wrong_typ":
        token = jwt.encode(_claims(typ="refresh"), CI_JWT_SECRET, algorithm="HS256")
    elif variant == "alg_none":
        token = jwt.encode(claims, None, algorithm="none")  # type: ignore[arg-type]
    elif variant == "no_tid":
        claims.pop("tid")
        token = jwt.encode(claims, CI_JWT_SECRET, algorithm="HS256")
    elif variant == "empty":
        return {"Authorization": "Bearer "}
    elif variant == "basic_scheme":
        return {"Authorization": "Basic b3duZXJAYWNtZS50ZXN0OnRlc3QtcGFzc3dvcmQtMDEyMw=="}
    else:
        raise ValueError(variant)
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("op", NON_PUBLIC, ids=ids_of(NON_PUBLIC))
async def test_api2_bad_tokens_401(api: httpx.AsyncClient, op: Operation) -> None:
    """Every variant in `VARIANTS` on the op (one test per op: the 401 comes before the DB)."""
    from tests.api_requests import valid_request

    for variant in VARIANTS:
        call = valid_request(op)
        response = await send(api, call, bad_header(variant))
        assert response.status_code == 401, (op.op_id, variant, response.text)
        body = response.json()
        assert body["error"]["type"] == "UNAUTHENTICATED", (variant, body)
        assert body["error"]["correlation_id"]
        challenge = response.headers.get("www-authenticate", "")
        assert "jwt" not in challenge.lower() and "decode" not in challenge.lower(), challenge
        assert "Traceback" not in response.text


async def _login(api: httpx.AsyncClient, email: str, password: str = PASSWORD) -> httpx.Response:
    return await api.post(LOGIN, json={"email": email, "password": password})


async def test_api2_refresh_reuse_revokes_family(api: httpx.AsyncClient, world2: World2) -> None:
    first = (await _login(api, "operator@acme.test")).json()
    rotated = await api.post(REFRESH, json={"refresh_token": first["refresh_token"]})
    assert rotated.status_code == 200, rotated.text
    reuse = await api.post(REFRESH, json={"refresh_token": first["refresh_token"]})
    assert reuse.status_code == 401
    assert reuse.json()["error"]["type"] == "UNAUTHENTICATED"
    # the reuse revoked the whole family: the rotated token is dead too
    after = await api.post(REFRESH, json={"refresh_token": rotated.json()["refresh_token"]})
    assert after.status_code == 401, after.text


async def test_api2_logout_then_refresh_401(api: httpx.AsyncClient, world2: World2) -> None:
    pair = (await _login(api, "reviewer@acme.test")).json()
    out = await api.post(LOGOUT, json={"refresh_token": pair["refresh_token"]})
    assert out.status_code == 204, out.text
    again = await api.post(REFRESH, json={"refresh_token": pair["refresh_token"]})
    assert again.status_code == 401


async def test_api2_sixth_login_429(api: httpx.AsyncClient, world2: World2) -> None:
    statuses = []
    for n in range(6):
        password = PASSWORD if n % 2 == 0 else "wrong-password-s26"
        statuses.append((await _login(api, "admin@acme.test", password)).status_code)
    assert all(s in (200, 401) for s in statuses[:5]), statuses
    assert statuses[5] == 429, statuses
    sixth = await _login(api, "admin@acme.test")
    assert sixth.status_code == 429
    assert sixth.json()["error"]["type"] == "RATE_LIMITED"
    assert 1 <= int(sixth.headers["retry-after"]) <= 60


async def test_api2_login_unknown_and_wrong_password_same_body_and_status(
    api: httpx.AsyncClient, world2: World2
) -> None:
    unknown = await _login(api, "nobody@acme.test", "wrong-password-s26")
    wrong = await _login(api, "owner@acme.test", "wrong-password-s26")
    assert unknown.status_code == wrong.status_code == 401

    def shape(response: httpx.Response) -> dict[str, Any]:
        error = dict(response.json()["error"])
        error.pop("correlation_id")
        report = dict(error.get("error_report") or {})
        report.pop("request_id", None)
        error["error_report"] = report
        return error

    assert shape(unknown) == shape(wrong)


async def test_api2_deactivated_member_token_401_or_403(
    api: httpx.AsyncClient, world2: World2
) -> None:
    headers = world2.headers("operator@acme.test")
    assert (await api.get("/api/v1/products", headers=headers)).status_code == 200
    world2.base.conn.execute(
        "UPDATE tenant_memberships SET is_active = false, deactivated_at = now() "
        "WHERE user_id = %s",
        [world2.base.user("operator@acme.test")],
    )
    refused = await api.get("/api/v1/products", headers=headers)
    assert refused.status_code in (401, 403), refused.text
    assert refused.json()["error"]["type"] in ("UNAUTHENTICATED", "PERMISSION_DENIED")


@pytest.mark.parametrize("case", ["non_member", "inactive_membership"])
async def test_api2_switch_tenant_non_member_or_inactive_404(
    api: httpx.AsyncClient, world2: World2, case: str
) -> None:
    email = "operator@acme.test"
    if case == "inactive_membership":
        add_member(
            world2.base.conn,
            world2.base.tenant("bharat"),
            world2.base.user(email),
            "operator",
            active=False,
        )
    response = await api.post(
        SWITCH,
        json={"target_tenant_id": str(world2.base.tenant("bharat"))},
        headers=world2.headers(email),
    )
    assert response.status_code == 404, response.text
    assert response.json()["error"]["type"] == "NOT_FOUND"
    for value in world2.foreign_values("bharat"):
        assert value not in response.text


async def test_api2_pre_switch_access_token_still_scoped_to_old_tid(
    api: httpx.AsyncClient, world2: World2
) -> None:
    email = "owner@acme.test"
    add_member(world2.base.conn, world2.base.tenant("bharat"), world2.base.user(email), "operator")
    old = (await _login(api, email)).json()
    switched = await api.post(
        SWITCH,
        json={"target_tenant_id": str(world2.base.tenant("bharat"))},
        headers={"Authorization": f"Bearer {old['access_token']}"},
    )
    assert switched.status_code == 200, switched.text
    new = switched.json()

    async def products(token: str) -> str:
        response = await api.get("/api/v1/products", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200, response.text
        return response.text

    old_view = await products(old["access_token"])
    new_view = await products(new["access_token"])
    assert world2.side("acme").canary in old_view
    assert world2.side("bharat").canary not in old_view
    assert world2.side("bharat").canary in new_view
    assert world2.side("acme").canary not in new_view
