"""S18 T10 (AC 9, 10; D2; CANON.md:152; DATABASE.md:802, :1386, :1402): the regenerate service and
the `force_regenerate` branch of generate, against the demo seed as `app_rw`."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import Marketplace

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world, set_status

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]
OPERATOR = "operator@acme.test"


def _listing() -> uuid.UUID:
    from app.db.seed_ops import demo_listing_ids

    return demo_listing_ids()[0]


async def _regenerate(
    world: GenWorld,
    listing: uuid.UUID,
    instructions: str | None = None,
    *,
    email: str = OPERATOR,
) -> Any:
    from app.repositories.uow import tenant_uow
    from app.services.generation import request_regeneration

    principal = world.principal(email)
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        return await request_regeneration(uow, principal, str(uuid.uuid4()), listing, instructions)


def _approve_current(world: GenWorld, listing: uuid.UUID) -> None:
    """An approved listing: the reviewer's approval on the current version (admin login)."""
    reviewer = world.user("reviewer@acme.test")
    with world.conn.transaction():
        world.conn.execute("SET LOCAL session_replication_role = replica")
        world.conn.execute(
            "UPDATE listing_versions SET approved_by_user_id = %s, approved_at = now() "
            "WHERE id = (SELECT current_version_id FROM listings WHERE id = %s)",
            [reviewer, listing],
        )
        world.conn.execute(
            "UPDATE listings SET approved_version_id = current_version_id WHERE id = %s", [listing]
        )


@pytest.mark.parametrize(
    "status", ["validated", "needs_review", "approved", "blocked", "failed", "published"]
)
async def test_regenerate_from_each_status(gen_world: GenWorld, status: str) -> None:
    listing = _listing()
    if status == "approved":
        _approve_current(gen_world, listing)
    set_status(gen_world, listing, status)
    accepted = await _regenerate(gen_world, listing, "Shorter title, please.")
    row = gen_world.listing(listing)
    assert (row["status"], row["last_event"], row["approved_version_id"]) == (
        "generating",
        "regenerate",
        None,
    )
    decision = gen_world.conn.execute(
        "SELECT decision, listing_version_id, instructions, regeneration_job_id, "
        "decided_by_user_id FROM review_decisions WHERE listing_id = %s",
        [listing],
    ).fetchone()
    assert decision == (
        "regenerate_requested",
        row["current_version_id"],
        "Shorter title, please.",
        accepted.job_id,
        gen_world.user(OPERATOR),
    )
    assert gen_world.scalar("SELECT status FROM job_runs WHERE id = %s", [accepted.job_id]) == (
        "queued"
    )


async def test_regenerate_failed_no_version_decision_null_version(gen_world: GenWorld) -> None:
    listing = _listing()
    gen_world.conn.execute("UPDATE listings SET current_version_id = NULL WHERE id = %s", [listing])
    set_status(gen_world, listing, "failed")
    await _regenerate(gen_world, listing)
    assert (
        gen_world.scalar(
            "SELECT listing_version_id FROM review_decisions WHERE listing_id = %s", [listing]
        )
        is None
    )
    assert gen_world.listing(listing)["status"] == "generating"


@pytest.mark.parametrize(
    ("arrange", "code"),
    [
        ("generating", ErrorCode.LISTING_BUSY),
        ("created", ErrorCode.INVALID_STATUS_TRANSITION),
        ("paused", ErrorCode.LISTING_PAUSED),
        ("archived", ErrorCode.LISTING_ARCHIVED),
    ],
)
async def test_regenerate_refused_409(gen_world: GenWorld, arrange: str, code: ErrorCode) -> None:
    from app.core.errors import ConflictError

    listing = _listing()
    if arrange == "generating":
        set_status(gen_world, listing, "generating")
    elif arrange in ("paused", "archived"):
        set_status(gen_world, listing, "validated")
        gen_world.conn.execute(
            f"UPDATE listings SET {arrange}_at = now(), {arrange}_by_user_id = %s WHERE id = %s",  # noqa: S608
            [gen_world.user("owner@acme.test"), listing],
        )
    with pytest.raises(ConflictError) as caught:
        await _regenerate(gen_world, listing)
    assert caught.value.code is code
    assert gen_world.count("review_decisions") == 0
    assert gen_world.count("job_runs") == 0


async def test_regenerate_unknown_and_other_tenant_404(gen_world: GenWorld) -> None:
    from app.core.errors import NotFoundError

    with pytest.raises(NotFoundError):
        await _regenerate(gen_world, uuid.uuid4())
    with pytest.raises(NotFoundError):
        await _regenerate(gen_world, _listing(), email="operator@bharat.test")


async def test_regenerate_reviewer_allowed(gen_world: GenWorld) -> None:
    listing = _listing()
    set_status(gen_world, listing, "needs_review")
    await _regenerate(gen_world, listing, email="reviewer@acme.test")
    assert gen_world.scalar(
        "SELECT decided_by_user_id FROM review_decisions WHERE listing_id = %s", [listing]
    ) == gen_world.user("reviewer@acme.test")


async def test_force_regenerate_takes_regenerate_path(gen_world: GenWorld) -> None:
    from app.repositories.uow import tenant_uow
    from app.services.generation import GenerateItem, ItemAccepted, request_generation

    listing = _listing()
    set_status(gen_world, listing, "validated")
    principal = gen_world.principal(OPERATOR)
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        [result] = await request_generation(
            uow,
            principal,
            str(uuid.uuid4()),
            [
                GenerateItem(
                    product_id=gen_world.product("acme", "KURTA-001"),
                    marketplace=Marketplace.AMAZON,
                    user_instructions="Mention the dupatta.",
                    force_regenerate=True,
                )
            ],
        )
    assert isinstance(result, ItemAccepted)
    row = gen_world.listing(listing)
    assert (row["status"], row["last_event"]) == ("generating", "regenerate")
    assert (
        gen_world.scalar(
            "SELECT instructions FROM review_decisions WHERE regeneration_job_id = %s",
            [result.job_id],
        )
        == "Mention the dupatta."
    )


async def test_regenerate_decision_row_same_tx(
    gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the decision row in the same transaction the events guard refuses the move."""
    from app.core.errors import ConflictError
    from app.repositories.review_decisions import ReviewDecisionRepository

    async def skipped(self: Any, *args: Any, **kwargs: Any) -> uuid.UUID:
        return uuid.uuid4()

    monkeypatch.setattr(ReviewDecisionRepository, "insert_regenerate_requested", skipped)
    listing = _listing()
    set_status(gen_world, listing, "validated")
    with pytest.raises(ConflictError) as caught:
        await _regenerate(gen_world, listing)
    assert caught.value.code is ErrorCode.INVALID_STATUS_TRANSITION
    assert gen_world.listing(listing)["status"] == "validated"


async def test_regenerate_secret_shaped_instructions_422_no_rows(gen_world: GenWorld) -> None:
    from app.core.errors import InputError

    listing = _listing()
    set_status(gen_world, listing, "validated")
    with pytest.raises(InputError) as caught:
        await _regenerate(gen_world, listing, 'use {"client_secret": "x-sentinel-2"}')
    assert caught.value.field == "instructions"
    assert gen_world.count("review_decisions") == 0
    assert gen_world.count("job_runs") == 0
    assert gen_world.listing(listing)["status"] == "validated"
