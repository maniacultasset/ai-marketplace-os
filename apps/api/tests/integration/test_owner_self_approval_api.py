"""ADR-0031 (owner decision 2026-11-11): the brand setting "Owners may approve their own
listings" through the real app as app_rw. Part 1: `PATCH /api/v1/tenants/me` with
`owner_self_approval` (owner only, step-up, audit). Part 2: the approve path and the review read
(an owner's own work under the setting, recorded; a reviewer's never; the hints)."""

from __future__ import annotations

import time
import uuid
from typing import Any

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world
from .products_support import bearer_for
from .review_support import (
    OPERATOR,
    OWNER,
    REVIEWER,
    Made,
    audits,
    edited,
    generated,
    item,
    only_item,
    post_approve,
    run_job,
)

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]
TENANT_URL = "/api/v1/tenants/me"
LISTINGS = "/api/v1/listings"
ADMIN = "admin@acme.test"
MKC = (403, "MAKER_CHECKER_VIOLATION")


def _setting(world: GenWorld) -> bool:
    return bool(
        world.conn.execute(
            "SELECT owner_self_approval FROM tenants WHERE id = %s", [world.tenant("acme")]
        ).fetchone()[
            0
        ]  # type: ignore[index]
    )


def _turn_on(world: GenWorld, on: bool = True) -> None:
    world.conn.execute(
        "UPDATE tenants SET owner_self_approval = %s WHERE id = %s", [on, world.tenant("acme")]
    )


async def _patch(
    api: httpx.AsyncClient,
    world: GenWorld,
    body: dict[str, Any],
    email: str = OWNER,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    auth = headers or bearer_for(world, email)
    tag = (await api.get(TENANT_URL, headers=auth)).headers["ETag"]
    return await api.patch(TENANT_URL, json=body, headers={**auth, "If-Match": tag})


def _setting_audits(world: GenWorld) -> list[tuple[Any, ...]]:
    return world.conn.execute(
        "SELECT actor_type, actor_user_id, entity_type, entity_id, before, after FROM audit_log "
        "WHERE action = 'tenant.owner_self_approval_changed' ORDER BY occurred_at"
    ).fetchall()


def _code(result: dict[str, Any]) -> tuple[int, str | None]:
    error = result["error"]
    return result["http_status"], None if error is None else error["error_code"]


# ---- part 1: the setting ----


async def test_setting_reads_off_by_default(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    for email in (OWNER, OPERATOR, REVIEWER, ADMIN):
        response = await api.get(TENANT_URL, headers=bearer_for(gen_world, email))
        assert response.status_code == 200, response.text
        assert response.json()["owner_self_approval"] is False


async def test_owner_turns_it_on_and_off_audited(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    response = await _patch(api, gen_world, {"owner_self_approval": True})
    assert response.status_code == 200, response.text
    assert response.json()["owner_self_approval"] is True and _setting(gen_world)
    response = await _patch(api, gen_world, {"owner_self_approval": False})
    assert response.status_code == 200, response.text
    assert not _setting(gen_world)
    owner, tenant = gen_world.user(OWNER), gen_world.tenant("acme")
    assert _setting_audits(gen_world) == [
        (
            "user",
            owner,
            "tenant",
            tenant,
            {"owner_self_approval": False},
            {"owner_self_approval": True},
        ),
        (
            "user",
            owner,
            "tenant",
            tenant,
            {"owner_self_approval": True},
            {"owner_self_approval": False},
        ),
    ]
    # a setting-only change writes no profile row
    assert gen_world.count("audit_log", "action = 'tenant.profile_updated'", []) == 0


async def test_unchanged_value_writes_no_audit(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    response = await _patch(api, gen_world, {"owner_self_approval": False})
    assert response.status_code == 200, response.text
    assert _setting_audits(gen_world) == []


async def test_setting_with_a_profile_field_writes_both_rows(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    response = await _patch(api, gen_world, {"owner_self_approval": True, "tone": "Warm."})
    assert response.status_code == 200, response.text
    assert len(_setting_audits(gen_world)) == 1
    before, after = gen_world.conn.execute(
        "SELECT before, after FROM audit_log WHERE action = 'tenant.profile_updated'"
    ).fetchone()  # type: ignore[misc]
    assert before == {"tone": None} and after == {"tone": "Warm."}


@pytest.mark.parametrize("email", [ADMIN, OPERATOR, REVIEWER])
async def test_only_an_owner_changes_it(
    api: httpx.AsyncClient, gen_world: GenWorld, email: str
) -> None:
    response = await _patch(api, gen_world, {"owner_self_approval": True}, email=email)
    assert response.status_code == 403, response.text
    assert response.json()["error"]["type"] == "PERMISSION_DENIED"
    assert not _setting(gen_world) and _setting_audits(gen_world) == []


async def test_admin_still_edits_the_profile(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    response = await _patch(api, gen_world, {"tone": "Calm."}, email=ADMIN)
    assert response.status_code == 200, response.text
    assert response.json()["owner_self_approval"] is False


async def test_null_is_422(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    for value in (None, "true", 1):
        response = await _patch(api, gen_world, {"owner_self_approval": value})
        assert response.status_code == 422, (value, response.text)
    assert not _setting(gen_world)


async def test_demoted_after_the_probe_is_refused(
    api: httpx.AsyncClient, gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The service reads the membership again in its transaction: an owner demoted between the
    request's probe and the update is refused, and nothing changes."""
    from app.services import tenants

    from . import factories as f

    f.member(gen_world.conn, gen_world.tenant("acme"), role="owner")  # the last-owner rule holds
    original = tenants._check_voice

    def demote(fields: Any) -> None:
        gen_world.conn.execute(
            "UPDATE tenant_memberships SET role = 'admin' WHERE user_id = %s",
            [gen_world.user(OWNER)],
        )
        original(fields)

    monkeypatch.setattr(tenants, "_check_voice", demote)
    auth = bearer_for(gen_world, OWNER)
    tag = (await api.get(TENANT_URL, headers=auth)).headers["ETag"]
    response = await api.patch(
        TENANT_URL, json={"owner_self_approval": True}, headers={**auth, "If-Match": tag}
    )
    assert response.status_code == 403, response.text
    assert not _setting(gen_world) and _setting_audits(gen_world) == []


def _fresh(world: GenWorld, email: str, *, at: int) -> dict[str, str]:
    from .auth_support import bearer, tokens_for

    return bearer(tokens_for(world, email, "acme", amr=["pwd", "otp"], mfa_at=at))  # type: ignore[arg-type]


def _factor(world: GenWorld, email: str) -> None:
    world.conn.execute(
        "INSERT INTO user_mfa_factors (user_id, kind, status, secret_blob, key_version, "
        "confirmed_at) VALUES (%s, 'totp', 'active', %s, 'local-v1', now())",
        [world.user(email), b"x" * 40],
    )


async def test_step_up_required_under_mfa_all(
    api: httpx.AsyncClient, gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from .mfa_support import mfa_on

    _factor(gen_world, OWNER)
    mfa_on(monkeypatch)
    stale = _fresh(gen_world, OWNER, at=int(time.time()) - 3600)
    response = await _patch(api, gen_world, {"owner_self_approval": True}, headers=stale)
    assert response.status_code == 403, response.text
    assert response.json()["error"]["type"] == "MFA_STEP_UP_REQUIRED"
    assert not _setting(gen_world)
    # a profile edit on the same stale session never asks for a code
    assert (await _patch(api, gen_world, {"tone": "Plain."}, headers=stale)).status_code == 200
    fresh = _fresh(gen_world, OWNER, at=int(time.time()) - 60)
    response = await _patch(api, gen_world, {"owner_self_approval": True}, headers=fresh)
    assert response.status_code == 200, response.text
    assert _setting(gen_world) and len(_setting_audits(gen_world)) == 1


async def test_enrollment_required_without_a_factor(
    api: httpx.AsyncClient, gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from .mfa_support import mfa_on

    mfa_on(monkeypatch)
    stale = _fresh(gen_world, OWNER, at=int(time.time()) - 3600)
    response = await _patch(api, gen_world, {"owner_self_approval": True}, headers=stale)
    assert response.status_code == 403, response.text
    assert response.json()["error"]["type"] == "MFA_ENROLLMENT_REQUIRED"


async def test_wrong_role_answers_before_the_step_up(
    api: httpx.AsyncClient, gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from .mfa_support import mfa_on

    _factor(gen_world, ADMIN)
    mfa_on(monkeypatch)
    stale = _fresh(gen_world, ADMIN, at=int(time.time()) - 3600)
    response = await _patch(api, gen_world, {"owner_self_approval": True}, headers=stale)
    assert response.json()["error"]["type"] == "PERMISSION_DENIED"


# ---- part 2: the approve path and the review read ----


async def _detail(
    api: httpx.AsyncClient, world: GenWorld, made: Made, email: str
) -> dict[str, Any]:
    response = await api.get(f"{LISTINGS}/{made.listing_id}", headers=bearer_for(world, email))
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _hint(body: dict[str, Any]) -> tuple[Any, Any, Any]:
    return body["can_approve"], body["approve_hint"], body["self_approval"]


def _deactivate(world: GenWorld, email: str) -> None:
    world.conn.execute(
        "UPDATE tenant_memberships SET is_active = false, deactivated_at = now() "
        "WHERE user_id = %s",
        [world.user(email)],
    )


def _self_flags(world: GenWorld, made: Made) -> list[bool]:
    """`self_approved` of the listing's approved versions (only lv_guard writes it)."""
    return [
        r[0]
        for r in world.conn.execute(
            "SELECT self_approved FROM listing_versions WHERE listing_id = %s "
            "AND approved_by_user_id IS NOT NULL",
            [made.listing_id],
        ).fetchall()
    ]


async def test_owner_alone_is_told_it_can_enable(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world, OWNER)
    _deactivate(gen_world, REVIEWER)
    assert _hint(await _detail(api, gen_world, made, OWNER)) == (False, "owner_can_enable", False)
    assert _code(only_item(await post_approve(api, gen_world, OWNER, [item(made)]))) == MKC


async def test_owner_self_approves_under_the_setting_recorded(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world, OWNER)
    _deactivate(gen_world, REVIEWER)
    _turn_on(gen_world)
    assert _hint(await _detail(api, gen_world, made, OWNER)) == (True, None, True)
    result = only_item(await post_approve(api, gen_world, OWNER, [item(made)]))
    assert _code(result) == (200, None) and result["listing_status"] == "approved"
    assert gen_world.listing(made.listing_id)["approved_version_id"] == made.version_id
    assert _self_flags(gen_world, made) == [True]
    assert audits(gen_world, made.listing_id, "listing.approved") == 1
    assert audits(gen_world, made.listing_id, "listing.self_approved") == 1
    row = gen_world.conn.execute(
        "SELECT actor_user_id, after, reason FROM audit_log WHERE entity_id = %s "
        "AND action = 'listing.self_approved'",
        [made.listing_id],
    ).fetchone()
    assert row == (gen_world.user(OWNER), {"status": "approved"}, "owner_self_approval")
    history = await api.get(
        f"{LISTINGS}/{made.listing_id}/history", headers=bearer_for(gen_world, OWNER)
    )
    assert history.status_code == 200, history.text
    events = {e["action"]: e for e in history.json()["data"]}
    assert events["listing.approved"]["review"]["self_approval"] is True
    assert events["listing.self_approved"]["review"] is None


async def test_owner_editor_self_approves_under_the_setting(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await edited(api, gen_world, await generated(gen_world, OPERATOR), OWNER)
    _turn_on(gen_world)
    assert _hint(await _detail(api, gen_world, made, OWNER)) == (True, None, True)
    assert _code(only_item(await post_approve(api, gen_world, OWNER, [item(made)]))) == (200, None)
    assert _self_flags(gen_world, made) == [True]


async def test_other_approver_is_never_a_self_approval(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world, OPERATOR)
    _turn_on(gen_world)
    assert _hint(await _detail(api, gen_world, made, OWNER)) == (True, None, False)
    assert _code(only_item(await post_approve(api, gen_world, OWNER, [item(made)]))) == (200, None)
    assert _self_flags(gen_world, made) == [False]
    assert audits(gen_world, made.listing_id, "listing.self_approved") == 0


async def test_reviewer_never_self_approves_under_the_setting(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world, OPERATOR)
    headers = {**bearer_for(gen_world, REVIEWER), "Idempotency-Key": str(uuid.uuid4())}
    response = await api.post(f"{LISTINGS}/{made.listing_id}/regenerate", headers=headers)
    assert response.status_code == 202, response.text
    await run_job(uuid.UUID(response.json()["job_id"]))
    regen = Made(
        made.listing_id, gen_world.listing(made.listing_id)["current_version_id"], made.job_id
    )
    _turn_on(gen_world)
    # the owner is outside a full regeneration's chain, so the reviewer is only `own_work`
    assert _hint(await _detail(api, gen_world, regen, REVIEWER)) == (False, "own_work", False)
    assert _code(only_item(await post_approve(api, gen_world, REVIEWER, [item(regen)]))) == MKC
    assert audits(gen_world, regen.listing_id, "listing.approve_denied") == 1


async def test_reviewer_alone_keeps_no_other_approver(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    """A non-owner with nobody else able to approve keeps `no_other_approver`, setting or not:
    the only owner does not count while its email is unverified (V1 hardening #12)."""
    made = await generated(gen_world, OPERATOR)
    headers = {**bearer_for(gen_world, REVIEWER), "Idempotency-Key": str(uuid.uuid4())}
    response = await api.post(f"{LISTINGS}/{made.listing_id}/regenerate", headers=headers)
    assert response.status_code == 202, response.text
    await run_job(uuid.UUID(response.json()["job_id"]))
    regen = Made(
        made.listing_id, gen_world.listing(made.listing_id)["current_version_id"], made.job_id
    )
    gen_world.conn.execute(
        "UPDATE users SET email_unverified = true WHERE id = %s", [gen_world.user(OWNER)]
    )
    for on in (False, True):
        _turn_on(gen_world, on)
        assert _hint(await _detail(api, gen_world, regen, REVIEWER)) == (
            False,
            "no_other_approver",
            False,
        )


@pytest.mark.parametrize("email", [OPERATOR, ADMIN])
async def test_operator_and_admin_never_approve_under_the_setting(
    api: httpx.AsyncClient, gen_world: GenWorld, email: str
) -> None:
    made = await generated(gen_world, OWNER if email == ADMIN else OPERATOR)
    _turn_on(gen_world)
    assert _hint(await _detail(api, gen_world, made, email)) == (False, "not_approver", False)
    response = await post_approve(api, gen_world, email, [item(made)])
    assert response.status_code == 403, response.text
    assert response.json()["error"]["type"] == "PERMISSION_DENIED"


async def test_setting_off_again_restores_maker_checker(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world, OWNER)
    _turn_on(gen_world)
    _turn_on(gen_world, False)
    assert _hint(await _detail(api, gen_world, made, OWNER)) == (False, "own_work", False)
    assert _code(only_item(await post_approve(api, gen_world, OWNER, [item(made)]))) == MKC


async def test_setting_turned_off_after_the_read_is_refused(
    api: httpx.AsyncClient, gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The setting is read inside the approve transaction: switched off after the request
    started (before the maker-checker step), the self-approval is refused and audited."""
    import app.services.review as review

    made = await generated(gen_world, OWNER)
    _turn_on(gen_world)
    original = review.current_pins  # type: ignore[attr-defined]

    async def switch_off(uow: Any, listing: Any) -> Any:
        _turn_on(gen_world, False)
        return await original(uow, listing)

    monkeypatch.setattr(review, "current_pins", switch_off)
    assert _code(only_item(await post_approve(api, gen_world, OWNER, [item(made)]))) == MKC
    assert audits(gen_world, made.listing_id, "listing.approve_denied") == 1


async def test_db_backstop_refuses_a_self_approval_with_the_setting_off(
    api: httpx.AsyncClient, gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The API's setting read patched to on while the row says off: `lv_guard` (MKC01) refuses,
    the savepoint rolls back, and the denial is audited."""
    from app.repositories.tenants import TenantProfileRepository

    async def always_on(self: Any) -> bool:
        return True

    monkeypatch.setattr(TenantProfileRepository, "owner_self_approval", always_on)
    made = await generated(gen_world, OWNER)
    assert _code(only_item(await post_approve(api, gen_world, OWNER, [item(made)]))) == MKC
    assert _self_flags(gen_world, made) == []
    assert audits(gen_world, made.listing_id, "listing.approve_denied") == 1
    assert audits(gen_world, made.listing_id, "listing.self_approved") == 0
