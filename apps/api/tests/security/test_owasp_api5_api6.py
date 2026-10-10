"""S26 T6 (AC 2, OWASP API5 + API6): function-level authorisation and business-flow abuse.

API5: every op refuses every role outside its grant with 403 `PERMISSION_DENIED` and no row
change; the staff admin is not mounted in production; the role comes from the membership row of
the token's tenant (a switch or a demotion takes effect on the next request).
API6: maker-checker in the API (refused before any `UPDATE listing_versions`) and in the database
(`lv_guard` MKC01); publish needs the approved, current version; the budget cap and the
submission cap hold.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from app.api.v1.surface import Operation
from tests.integration.auth_support import PASSWORD, ROLES, add_member
from tests.integration.conftest import expect_sqlstate
from tests.integration.generation_support import GenWorld, gen_world
from tests.integration.integrity_support import APPROVE_SQL, as_app_rw
from tests.integration.products_support import statements
from tests.integration.publish_support import PubWorld, approved, pub, publish_one, run_job
from tests.integration.review_support import (
    OPERATOR,
    OWNER,
    REVIEWER,
    decisions,
    edited,
    generated,
    item,
    only_item,
    post_approve,
)

from .conftest import World2, call_for, ids_of, ops, send, tenant_fingerprint

__all__ = ["gen_world", "pub"]
NON_PUBLIC = [op for op in ops(public=False) if set(ROLES) - set(op.roles or ())]
MKC = (403, "MAKER_CHECKER_VIOLATION")


def _updates_versions(seen: list[str]) -> list[str]:
    return [s for s in seen if s.lstrip().upper().startswith("UPDATE LISTING_VERSIONS")]


@pytest.mark.parametrize("op", NON_PUBLIC, ids=ids_of(NON_PUBLIC))
async def test_api5_denied_role_403(api: httpx.AsyncClient, world2: World2, op: Operation) -> None:
    from tests.support.rate_limits import reset_rate_limits

    conn = world2.base.conn
    acme = world2.side("acme").tenant_id
    before = tenant_fingerprint(conn, acme)
    for role in sorted(set(ROLES) - set(op.roles or ())):
        reset_rate_limits(conn)
        response = await send(
            api, call_for(op, world2, "acme"), world2.headers(f"{role}@acme.test")
        )
        assert response.status_code == 403, (op.op_id, role, response.text)
        assert response.json()["error"]["type"] == "PERMISSION_DENIED"
    assert tenant_fingerprint(conn, acme) == before, op.op_id


async def test_api5_internal_admin_404_in_production(prod_client: httpx.AsyncClient) -> None:
    for path in ("/internal/admin", "/internal/admin/", "/internal/admin/login"):
        response = await prod_client.get(path)
        assert response.status_code == 404, (path, response.text)
        assert response.json()["error"]["type"] == "NOT_FOUND"


async def test_api6_generator_approve_403_maker_checker(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world, OWNER)
    with statements() as seen:
        result = only_item(await post_approve(api, gen_world, OWNER, [item(made)]))
    assert (result["http_status"], result["error"]["error_code"]) == MKC
    assert _updates_versions(seen) == [], "the API let the approval reach SQL"
    assert decisions(gen_world, made.listing_id) == 0


async def test_api6_editor_approve_403(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    made = await edited(api, gen_world, await generated(gen_world), OWNER)
    with statements() as seen:
        result = only_item(await post_approve(api, gen_world, OWNER, [item(made)]))
    assert (result["http_status"], result["error"]["error_code"]) == MKC
    assert _updates_versions(seen) == []
    assert gen_world.listing(made.listing_id)["approved_version_id"] is None


async def test_api6_db_backstop_refuses_self_approval(gen_world: GenWorld) -> None:
    made = await generated(gen_world, OPERATOR)
    conn = gen_world.conn
    tenant = gen_world.tenant("acme")
    with conn.transaction(), as_app_rw(conn, tenant):
        expect_sqlstate(conn, "MKC01", APPROVE_SQL, (gen_world.user(OPERATOR), made.version_id))
    assert gen_world.listing(made.listing_id)["approved_version_id"] is None


async def test_api6_publish_not_approved_refused(api: httpx.AsyncClient, pub: PubWorld) -> None:
    made = await generated(pub.world, OPERATOR)
    result = await publish_one(api, pub, made)
    assert result["http_status"] in (409, 422), result
    assert result["job_id"] is None
    assert pub.submissions(made.listing_id) == []
    assert pub.fake.calls == []


async def test_api6_publish_stale_version_refused(api: httpx.AsyncClient, pub: PubWorld) -> None:
    made = await approved(pub)
    # content patched after the approval: a new current version nobody approved
    await edited(api, pub.world, made, OPERATOR)
    result = await publish_one(api, pub, made)
    assert result["http_status"] in (409, 422), result
    assert result["job_id"] is None
    assert pub.fake.calls == []


async def test_api6_budget_429(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    from datetime import UTC, datetime
    from decimal import Decimal

    import tests.integration.factories as f

    f.agent_run(
        gen_world.conn,
        gen_world.tenant("acme"),
        cost_usd=Decimal("2.500000"),
        started_at=datetime.now(UTC),
    )
    product = gen_world.product("acme", "KURTA-001")
    response = await api.post(
        "/api/v1/listings/generate",
        headers=gen_world.headers(OPERATOR),
        json={"product_id": str(product), "marketplace": "amazon"},
    )
    assert response.status_code == 429, response.text
    assert response.json()["error"]["type"] == "BUDGET_EXCEEDED"
    assert 1 <= int(response.headers["retry-after"]) <= 86400


async def test_api6_retry_fields_after_cap_refused(api: httpx.AsyncClient, pub: PubWorld) -> None:
    from app.adapters.base import RateLimited
    from mkos_schemas.errors import ErrorReport

    made = await approved(pub)
    pub.fake.raise_next = [RateLimited(ErrorReport(status="failed")) for _ in range(3)]
    first = await publish_one(api, pub, made)
    for _ in range(3):
        try:
            await run_job(uuid.UUID(first["job_id"]))
        except Exception as exc:  # RetryLater between the attempts
            assert type(exc).__name__ == "RetryLater"
    assert pub.listing(made.listing_id)["status"] == "blocked"
    writes = len(pub.fake.calls)
    retry = await api.post(
        f"/api/v1/listings/{made.listing_id}/publish/retry-fields",
        headers=pub.world.headers(OPERATOR),
    )
    assert retry.status_code in (409, 422), retry.text
    again = await publish_one(api, pub, made)
    assert (again["http_status"], again["error"]["error_code"]) == (
        409,
        "SUBMISSION_ATTEMPTS_EXHAUSTED",
    )
    assert len(pub.submissions(made.listing_id)) == 3
    assert len(pub.fake.calls) == writes


async def _login(api: httpx.AsyncClient, email: str) -> dict[str, Any]:
    response = await api.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()  # type: ignore[no-any-return]


async def test_api5_switch_role_is_target_membership_role(
    api: httpx.AsyncClient, world2: World2
) -> None:
    email = "owner@acme.test"
    add_member(world2.base.conn, world2.base.tenant("bharat"), world2.base.user(email), "operator")
    pair = await _login(api, email)
    switched = await api.post(
        "/api/v1/auth/switch-tenant",
        json={"target_tenant_id": str(world2.base.tenant("bharat"))},
        headers={"Authorization": f"Bearer {pair['access_token']}"},
    )
    assert switched.status_code == 200, switched.text
    headers = {"Authorization": f"Bearer {switched.json()['access_token']}"}
    owner_only = [
        op
        for op in ops(public=False)
        if "owner" in (op.roles or ()) and "operator" not in (op.roles or ())
    ]
    assert owner_only
    for op in owner_only:
        response = await send(api, call_for(op, world2, "bharat"), headers)
        assert response.status_code == 403, (op.op_id, response.text)


async def test_api5_demoted_reviewer_live_token_cannot_approve(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world, OPERATOR)
    headers = gen_world.headers(REVIEWER)  # a live access token, minted while a reviewer
    gen_world.conn.execute(
        "UPDATE tenant_memberships SET role = 'operator' WHERE user_id = %s AND tenant_id = %s",
        [gen_world.user(REVIEWER), gen_world.tenant("acme")],
    )
    response = await api.post(
        "/api/v1/listings/approve", headers=headers, json={"items": [item(made)]}
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["type"] == "PERMISSION_DENIED"
    assert decisions(gen_world, made.listing_id) == 0
