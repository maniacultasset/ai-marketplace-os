"""S19 T9 (DW1, DW3; AC 2, 4; invariant 3; T15, T88): maker-checker in the API and in the
database (`lv_guard` MKC01), and who may approve."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from . import factories as f
from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world
from .products_support import statements
from .review_support import (
    OPERATOR,
    OWNER,
    REVIEWER,
    Made,
    audits,
    decisions,
    edited,
    generated,
    item,
    only_item,
    post_approve,
    run_job,
)

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]
MKC = (403, "MAKER_CHECKER_VIOLATION")


def _code(result: dict[str, Any]) -> tuple[int, str | None]:
    error = result["error"]
    return result["http_status"], None if error is None else error["error_code"]


def _approved(world: GenWorld, made: Made) -> bool:
    return bool(world.listing(made.listing_id)["approved_version_id"] == made.version_id)


async def test_api_maker_checker_generator(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    made = await generated(gen_world, OWNER)
    assert _code(only_item(await post_approve(api, gen_world, OWNER, [item(made)]))) == MKC
    assert audits(gen_world, made.listing_id, "listing.approve_denied") == 1
    reason = gen_world.scalar(
        "SELECT reason FROM audit_log WHERE entity_id = %s AND action = 'listing.approve_denied'",
        [made.listing_id],
    )
    assert reason == "MAKER_CHECKER_VIOLATION"
    assert not _approved(gen_world, made) and decisions(gen_world, made.listing_id) == 0


async def test_api_maker_checker_latest_editor(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    made = await edited(api, gen_world, await generated(gen_world), OWNER)
    assert _code(only_item(await post_approve(api, gen_world, OWNER, [item(made)]))) == MKC
    assert _code(only_item(await post_approve(api, gen_world, REVIEWER, [item(made)]))) == (
        200,
        None,
    )


async def test_api_maker_checker_earlier_editor(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world, OPERATOR)
    made = await edited(api, gen_world, made, OWNER)
    made = await edited(api, gen_world, made, OPERATOR)
    assert _code(only_item(await post_approve(api, gen_world, OWNER, [item(made)]))) == MKC
    assert _code(only_item(await post_approve(api, gen_world, REVIEWER, [item(made)])))[0] == 200


async def test_api_maker_checker_regenerator(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    from .products_support import bearer_for

    made = await generated(gen_world, OPERATOR)
    headers = {**bearer_for(gen_world, REVIEWER), "Idempotency-Key": str(uuid.uuid4())}
    response = await api.post(f"/api/v1/listings/{made.listing_id}/regenerate", headers=headers)
    assert response.status_code == 202, response.text
    await run_job(uuid.UUID(response.json()["job_id"]))
    row = gen_world.listing(made.listing_id)
    regen = Made(made.listing_id, row["current_version_id"], made.job_id)
    assert row["status"] == "validated" and regen.version_id != made.version_id
    assert _code(only_item(await post_approve(api, gen_world, REVIEWER, [item(regen)]))) == MKC
    # a full regeneration resets the chain: the first maker (the operator) is out of it, and
    # the owner, who never touched it, approves
    assert _code(only_item(await post_approve(api, gen_world, OWNER, [item(regen)])))[0] == 200


async def test_api_maker_checker_refuses_before_sql(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world, OWNER)
    with statements() as seen:
        assert _code(only_item(await post_approve(api, gen_world, OWNER, [item(made)]))) == MKC
    assert not [s for s in seen if s.lstrip().upper().startswith("UPDATE LISTING_VERSIONS")]


async def test_db_maker_checker_backstop(
    api: httpx.AsyncClient, gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The API check patched out: `lv_guard` refuses (MKC01), the item's savepoint rolls back,
    the denial is audited, and the other item of the batch is kept."""
    import app.services.review as review

    monkeypatch.setattr(review, "approver_refusal", lambda *_a: None)
    mine = await generated(gen_world, OWNER)
    other = await generated(gen_world, OPERATOR)
    response = await post_approve(api, gen_world, OWNER, [item(mine), item(other)])
    assert response.status_code == 200, response.text
    first, second = response.json()["items"]
    assert _code(first) == MKC
    assert _code(second) == (200, None)
    assert not _approved(gen_world, mine) and _approved(gen_world, other)
    assert decisions(gen_world, mine.listing_id) == 0
    assert (
        gen_world.scalar(
            "SELECT approved_by_user_id FROM listing_versions WHERE id = %s", [mine.version_id]
        )
        is None
    )
    assert audits(gen_world, mine.listing_id, "listing.approve_denied") == 1


async def test_approver_roles(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    """T15: an owner who generated never approves alone; a second owner or a reviewer can;
    T88: admin and operator cannot approve at all (PERMISSION_DENIED)."""
    from .auth_support import bearer, tokens_for

    made = await generated(gen_world, OWNER)
    assert _code(only_item(await post_approve(api, gen_world, OWNER, [item(made)]))) == MKC
    second = f.member(gen_world.conn, gen_world.tenant("acme"), role="owner")
    email = gen_world.scalar("SELECT email FROM users WHERE id = %s", [second])
    gen_world.users[email] = second
    headers = {**bearer(tokens_for(gen_world, email, "acme", "owner"))}  # type: ignore[arg-type]
    headers["Idempotency-Key"] = str(uuid.uuid4())
    response = await api.post(
        "/api/v1/listings/approve", headers=headers, json={"items": [item(made)]}
    )
    assert _code(only_item(response)) == (200, None)
    other = await generated(gen_world, OWNER)
    assert _code(only_item(await post_approve(api, gen_world, REVIEWER, [item(other)])))[0] == 200
    third = await generated(gen_world, OPERATOR)
    for email in ("admin@acme.test", OPERATOR):
        response = await post_approve(api, gen_world, email, [item(third)])
        assert response.status_code == 403
        assert response.json()["error"]["type"] == "PERMISSION_DENIED"


def _membership(world: GenWorld, email: str, **values: Any) -> None:
    sets = ", ".join(f"{k} = %s" for k in values)
    world.conn.execute(
        f"UPDATE tenant_memberships SET {sets} WHERE user_id = %s AND tenant_id = %s",  # noqa: S608
        [*values.values(), world.user(email), world.tenant("acme")],
    )


async def test_approver_inactive_401(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    made = await generated(gen_world, OPERATOR)
    _membership(gen_world, REVIEWER, is_active=False)
    response = await post_approve(api, gen_world, REVIEWER, [item(made)])
    assert response.status_code == 401
    assert gen_world.listing(made.listing_id)["status"] == "validated"


async def _change_after_probe(
    monkeypatch: pytest.MonkeyPatch, world: GenWorld, email: str, **values: Any
) -> None:
    """Commit a membership change after the request's auth probe passed and before the
    approve's `FOR SHARE` read (it runs at the pin check, which comes first)."""
    import app.services.review as review

    original = review.current_pins  # type: ignore[attr-defined]

    async def changed(uow: Any, listing: Any) -> Any:
        _membership(world, email, **values)
        return await original(uow, listing)

    monkeypatch.setattr(review, "current_pins", changed)


@pytest.mark.parametrize(
    "change", [{"is_active": False}, {"role": "operator"}], ids=["deactivated", "demoted"]
)
async def test_approver_changed_after_probe_403(
    api: httpx.AsyncClient,
    gen_world: GenWorld,
    monkeypatch: pytest.MonkeyPatch,
    change: dict[str, Any],
) -> None:
    made = await generated(gen_world, OPERATOR)
    await _change_after_probe(monkeypatch, gen_world, REVIEWER, **change)
    with statements() as seen:
        result = only_item(await post_approve(api, gen_world, REVIEWER, [item(made)]))
    assert _code(result) == MKC
    assert not [s for s in seen if s.lstrip().upper().startswith("UPDATE LISTING_VERSIONS")]
    assert audits(gen_world, made.listing_id, "listing.approve_denied") == 1
    assert not _approved(gen_world, made)
