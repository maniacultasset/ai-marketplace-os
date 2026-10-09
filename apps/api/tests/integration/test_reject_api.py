"""S19 T11 (DW5; AC 6; T40; DATABASE.md:791-806): reject needs a reason and feedback, moves the
listing to `blocked/rejected`; `regenerate=true` enqueues the regeneration in the same
transaction and makes the reviewer the maker of the new version."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world, set_status
from .review_support import (
    OPERATOR,
    OWNER,
    REVIEWER,
    Made,
    audits,
    decisions,
    generated,
    item,
    only_item,
    post_approve,
    run_job,
)

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]
FEEDBACK = "The title repeats the brand name; keep it under 80 characters."


def _reject(made: Made, **extra: Any) -> dict[str, Any]:
    return item(made, "reject", reason_code="poor_quality", feedback=FEEDBACK, **extra)


@pytest.mark.parametrize("missing", ["reason_code", "feedback"])
async def test_reject_missing_field_422_no_row(
    api: httpx.AsyncClient, gen_world: GenWorld, missing: str
) -> None:
    made = await generated(gen_world)
    body = _reject(made)
    del body[missing]
    response = await post_approve(api, gen_world, REVIEWER, [body])
    assert response.status_code == 422
    assert decisions(gen_world, made.listing_id, "reject") == 0
    assert gen_world.listing(made.listing_id)["status"] == "validated"


async def test_reject_blocked_rejected(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    response = await post_approve(api, gen_world, REVIEWER, [_reject(made)])
    result = only_item(response)
    assert (result["http_status"], result["listing_status"], result["job_id"]) == (
        200,
        "blocked",
        None,
    )
    row = gen_world.listing(made.listing_id)
    assert (row["status"], row["status_reason"], row["last_event"]) == (
        "blocked",
        "rejected",
        "reject",
    )
    decision = gen_world.conn.execute(
        "SELECT listing_version_id, reason_code, feedback, regeneration_job_id, batch_id "
        "FROM review_decisions WHERE listing_id = %s AND decision = 'reject'",
        [made.listing_id],
    ).fetchone()
    assert decision is not None
    assert decision[:4] == (made.version_id, "poor_quality", FEEDBACK, None)
    assert gen_world.scalar("SELECT kind FROM batches WHERE id = %s", [decision[4]]) == "reject"
    reason = gen_world.scalar(
        "SELECT reason FROM audit_log WHERE entity_id = %s AND action = 'listing.rejected'",
        [made.listing_id],
    )
    assert reason == "poor_quality"
    assert FEEDBACK not in str(
        gen_world.conn.execute(
            "SELECT before, after, reason FROM audit_log WHERE entity_id = %s",
            [made.listing_id],
        ).fetchall()
    )


async def test_reject_by_maker_allowed(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    """D7: maker-checker guards approval only; the maker may reject their own draft."""
    made = await generated(gen_world, OWNER)
    assert (
        only_item(await post_approve(api, gen_world, OWNER, [_reject(made)]))["http_status"] == 200
    )


async def test_reject_regenerate_job_policy(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    result = only_item(
        await post_approve(api, gen_world, REVIEWER, [_reject(made, regenerate=True)])
    )
    assert (result["http_status"], result["listing_status"]) == (200, "generating")
    job_id = uuid.UUID(result["job_id"])
    job = gen_world.conn.execute(
        "SELECT kind, status, created_by, max_attempts, listing_id FROM job_runs WHERE id = %s",
        [job_id],
    ).fetchone()
    assert job == ("listing_generate", "queued", gen_world.user(REVIEWER), 1, made.listing_id)
    row = gen_world.listing(made.listing_id)
    assert (row["status"], row["last_event"], row["approved_version_id"]) == (
        "generating",
        "reject_regenerate",
        None,
    )
    regen = gen_world.scalar(
        "SELECT regeneration_job_id FROM review_decisions WHERE listing_id = %s "
        "AND decision = 'reject'",
        [made.listing_id],
    )
    assert regen == job_id
    assert audits(gen_world, made.listing_id, "listing.rejected") == 1


async def test_reject_regenerate_one_transaction(
    api: httpx.AsyncClient, gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fault after the enqueue rolls everything back: no job, no decision, no move."""
    from app.repositories.review_decisions import ReviewDecisionRepository

    async def broken(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("injected fault after the enqueue")

    made = await generated(gen_world)
    jobs = gen_world.count("job_runs", "listing_id = %s", [made.listing_id])
    monkeypatch.setattr(ReviewDecisionRepository, "insert_reject", broken)
    response = await post_approve(api, gen_world, REVIEWER, [_reject(made, regenerate=True)])
    assert response.status_code == 500
    assert gen_world.count("job_runs", "listing_id = %s", [made.listing_id]) == jobs
    assert decisions(gen_world, made.listing_id, "reject") == 0
    assert gen_world.listing(made.listing_id)["status"] == "validated"
    assert gen_world.count("batches") == 0


async def test_reject_regenerate_budget_429_nothing_enqueued(
    api: httpx.AsyncClient, gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.services.review as review
    from app.services.budget import BudgetExceededError

    async def spent(*_a: Any) -> None:
        raise BudgetExceededError(60)

    made = await generated(gen_world)
    jobs = gen_world.count("job_runs", "listing_id = %s", [made.listing_id])
    monkeypatch.setattr(review, "precheck", spent)
    result = only_item(
        await post_approve(api, gen_world, REVIEWER, [_reject(made, regenerate=True)])
    )
    assert (result["http_status"], result["error"]["error_code"]) == (429, "BUDGET_EXCEEDED")
    assert gen_world.count("job_runs", "listing_id = %s", [made.listing_id]) == jobs
    assert decisions(gen_world, made.listing_id, "reject") == 0
    assert gen_world.listing(made.listing_id)["status"] == "validated"


async def test_reject_feedback_reaches_context(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    """The reject's feedback is the regeneration's `user_instructions` and joins
    `review_feedback` (S18's builder reads both through these two reads)."""
    from app.repositories.uow import tenant_uow

    made = await generated(gen_world)
    result = only_item(
        await post_approve(api, gen_world, REVIEWER, [_reject(made, regenerate=True)])
    )
    job_id = uuid.UUID(result["job_id"])
    principal = gen_world.principal(REVIEWER)
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        assert await uow.review_decisions.has_regenerate_for_job(job_id) is True
        assert await uow.review_decisions.instructions_for_job(job_id) == FEEDBACK
        assert await uow.review_decisions.latest_reject_feedback(made.listing_id, 5) == [FEEDBACK]


async def test_reject_regenerate_makes_reviewer_maker(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world, OPERATOR)
    result = only_item(
        await post_approve(api, gen_world, REVIEWER, [_reject(made, regenerate=True)])
    )
    await run_job(uuid.UUID(result["job_id"]))
    row = gen_world.listing(made.listing_id)
    assert row["status"] == "validated"
    version = gen_world.conn.execute(
        "SELECT origin, generated_by_user_id, changed_paths, parent_version_id "
        "FROM listing_versions WHERE id = %s",
        [row["current_version_id"]],
    ).fetchone()
    assert version == ("regeneration", gen_world.user(REVIEWER), None, made.version_id)
    regen = Made(made.listing_id, row["current_version_id"], made.job_id)
    refused = only_item(await post_approve(api, gen_world, REVIEWER, [item(regen)]))
    assert refused["error"]["error_code"] == "MAKER_CHECKER_VIOLATION"
    assert only_item(await post_approve(api, gen_world, OWNER, [item(regen)]))["http_status"] == 200


@pytest.mark.parametrize("status", ["blocked", "approved", "created"])
async def test_reject_from_other_status_409(
    api: httpx.AsyncClient, gen_world: GenWorld, status: str
) -> None:
    made = await generated(gen_world)
    set_status(
        gen_world, made.listing_id, status, "validation_errors" if status == "blocked" else None
    )
    result = only_item(await post_approve(api, gen_world, REVIEWER, [_reject(made)]))
    assert (result["http_status"], result["error"]["error_code"]) == (
        409,
        "INVALID_STATUS_TRANSITION",
    )


async def test_reject_regenerate_secret_feedback_422(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    """The feedback becomes prompt instructions: JSON with a secret-shaped key is refused before
    any write, as S18 refuses it in regenerate instructions (CP12)."""
    made = await generated(gen_world)
    body = item(
        made,
        "reject",
        reason_code="other",
        feedback='{"api_key": "sk-not-a-real-key-000000"}',
        regenerate=True,
    )
    response = await post_approve(api, gen_world, REVIEWER, [body])
    assert response.status_code == 422
    assert "sk-not-a-real-key" not in response.text
    assert gen_world.count("batches") == 0
