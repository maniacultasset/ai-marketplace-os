"""Client item 20 bullet 3 (12 Nov): the brand setting "Let Kuinn fix safe listing errors
automatically" through the real app as app_rw. `PATCH /api/v1/tenants/me` with
`auto_fix_enabled` (on by default, owner only, step-up, audited), and the seller's
`POST /listings/{id}/fixes/apply` refusals."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world
from .products_support import bearer_for
from .review_support import OPERATOR, OWNER, REVIEWER

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]
TENANT_URL = "/api/v1/tenants/me"
ADMIN = "admin@acme.test"
FIELD = "auto_fix_enabled"


def _setting(world: GenWorld) -> bool:
    row = world.conn.execute(
        "SELECT auto_fix_enabled FROM tenants WHERE id = %s", [world.tenant("acme")]
    ).fetchone()
    assert row is not None
    return bool(row[0])


async def _patch(
    api: httpx.AsyncClient, world: GenWorld, body: dict[str, Any], email: str = OWNER
) -> httpx.Response:
    auth = bearer_for(world, email)
    tag = (await api.get(TENANT_URL, headers=auth)).headers["ETag"]
    return await api.patch(TENANT_URL, json=body, headers={**auth, "If-Match": tag})


def _audits(world: GenWorld) -> list[tuple[Any, ...]]:
    return world.conn.execute(
        "SELECT actor_type, actor_user_id, entity_type, entity_id, before, after FROM audit_log "
        "WHERE action = 'tenant.auto_fix_enabled_changed' ORDER BY occurred_at"
    ).fetchall()


async def test_on_by_default_for_every_role(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    for email in (OWNER, OPERATOR, REVIEWER, ADMIN):
        response = await api.get(TENANT_URL, headers=bearer_for(gen_world, email))
        assert response.status_code == 200, response.text
        assert response.json()[FIELD] is True


async def test_owner_turns_it_off_and_on_audited(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    response = await _patch(api, gen_world, {FIELD: False})
    assert response.status_code == 200, response.text
    assert response.json()[FIELD] is False and not _setting(gen_world)
    response = await _patch(api, gen_world, {FIELD: True})
    assert response.status_code == 200, response.text
    assert _setting(gen_world)
    owner, tenant = gen_world.user(OWNER), gen_world.tenant("acme")
    assert _audits(gen_world) == [
        ("user", owner, "tenant", tenant, {FIELD: True}, {FIELD: False}),
        ("user", owner, "tenant", tenant, {FIELD: False}, {FIELD: True}),
    ]
    assert gen_world.count("audit_log", "action = 'tenant.profile_updated'", []) == 0
    assert gen_world.count("audit_log", "action = 'tenant.owner_self_approval_changed'", []) == 0


async def test_unchanged_value_writes_no_audit(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    response = await _patch(api, gen_world, {FIELD: True})
    assert response.status_code == 200, response.text
    assert _audits(gen_world) == []


@pytest.mark.parametrize("email", [ADMIN, OPERATOR, REVIEWER])
async def test_only_an_owner_changes_it(
    api: httpx.AsyncClient, gen_world: GenWorld, email: str
) -> None:
    response = await _patch(api, gen_world, {FIELD: False}, email=email)
    assert response.status_code == 403, response.text
    assert response.json()["error"]["type"] == "PERMISSION_DENIED"
    assert _setting(gen_world) and _audits(gen_world) == []


async def test_null_is_422(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    for value in (None, "false", 0):
        response = await _patch(api, gen_world, {FIELD: value})
        assert response.status_code == 422, (value, response.text)
    assert _setting(gen_world)
