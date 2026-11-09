"""SI11 fix round 2 (R2-1, R2-2): the human lanes' writers of the resolution executor. A step
the planner gives the seller, the marketplace or staff opens its task and links the issue, so
the listing never sits with an open marketplace issue that nobody is asked about."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource, Severity
from mkos_schemas.errors import ErrorItem

from .conftest import LOCAL_HOSTS
from .preview_support import prev, preview_jobs, preview_rows
from .publish_support import PubWorld, run_job
from .review_support import generated

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["prev"]


def _error(field: str, code: ErrorCode, raw: str, message: str) -> ErrorItem:
    return ErrorItem(
        field=field,
        error_code=code,
        severity=Severity.ERROR,
        message=message,
        source=ErrorSource.AMAZON_ADAPTER,
        retryable=False,
        raw_code=raw,
    )


async def _previewed(pw: PubWorld, errors: list[ErrorItem]) -> Any:
    pw.fake.preview_errors = errors
    made = await generated(pw.world)
    for job in preview_jobs(pw, made.listing_id):
        if job["status"] == "queued":
            await run_job(job["id"])
    return made


def _tasks(pw: PubWorld, listing_id: uuid.UUID) -> list[dict[str, Any]]:
    cur = pw.conn.execute(
        "SELECT t.kind, t.target_path, t.status, t.question, i.field_path "
        "FROM listing_issues i JOIN human_tasks t ON t.id = i.human_task_id "
        "WHERE i.listing_id = %s AND i.status = 'open' ORDER BY i.created_at",
        [listing_id],
    )
    names = [d.name for d in cur.description or ()]
    return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


async def test_a_qualification_error_opens_the_seller_central_step(prev: PubWorld) -> None:
    made = await _previewed(
        prev,
        [
            _error(
                "listing",
                ErrorCode.VALUE_NOT_ALLOWED,
                "99320",
                "You need approval to list products of this brand.",
            )
        ],
    )
    tasks = _tasks(prev, made.listing_id)
    category = prev.listing(made.listing_id)["category"]
    assert [(t["kind"], t["target_path"], t["status"]) for t in tasks] == [
        ("permission_decision", f"account.category_approvals.{category}", "open")
    ]
    assert "Seller Central" in tasks[0]["question"]
    # no automatic job: only the marketplace can act
    assert [j["status"] for j in preview_jobs(prev, made.listing_id)] == ["succeeded"]
    assert prev.listing(made.listing_id)["status"] == "blocked"


async def test_an_unknown_code_opens_the_staff_review(prev: PubWorld) -> None:
    made = await _previewed(
        prev,
        [
            _error(
                "listing",
                ErrorCode.MARKETPLACE_UNKNOWN_ERROR,
                "77777",
                "Something unusual went wrong with this listing.",
            )
        ],
    )
    tasks = _tasks(prev, made.listing_id)
    assert [(t["kind"], t["target_path"]) for t in tasks] == [
        ("marketplace_error_review", "account.error_review")
    ]


async def test_a_seller_step_asks_one_question_and_only_for_its_rows(prev: PubWorld) -> None:
    made = await _previewed(
        prev,
        [
            _error(
                "attributes.color",
                ErrorCode.VALUE_NOT_ALLOWED,
                "99300",
                "The value provided for 'color' is not one of the allowed values.",
            ),
            _error(
                "listing",
                ErrorCode.MARKETPLACE_UNKNOWN_ERROR,
                "77777",
                "Something unusual went wrong with this listing.",
            ),
        ],
    )
    tasks = {t["field_path"]: (t["kind"], t["target_path"]) for t in _tasks(prev, made.listing_id)}
    assert tasks["attributes.color"][0] == "missing_fact"
    assert tasks["attributes.color"][1].endswith("color")
    # the staff row never becomes a seller question (CP-D25)
    assert tasks["listing"] == ("marketplace_error_review", "account.error_review")
    row = prev.listing(made.listing_id)
    # the open row blocks; no question move, so the dry run and the revalidate stay reachable
    assert (row["status"], row["status_reason"]) == ("blocked", "validation_errors")


async def test_a_refused_value_is_never_offered_back(prev: PubWorld) -> None:
    """QA: the colour question after a 99300 refusal of Indigo offered Indigo again."""
    refused = _error(
        "attributes.color",
        ErrorCode.VALUE_NOT_ALLOWED,
        "99300",
        "The value provided for 'color' is not one of the allowed values.",
    ).model_copy(update={"value_submitted": "Blue"})
    made = await _previewed(prev, [refused])
    expected = prev.scalar(
        "SELECT t.expected_answer FROM human_tasks t JOIN listing_issues i "
        "ON i.human_task_id = t.id WHERE i.listing_id = %s AND i.field_path = 'attributes.color'",
        [made.listing_id],
    )
    allowed = [v.casefold() for v in expected["allowed_values"]]
    assert allowed and "blue" not in allowed
    assert len(allowed) == len(set(allowed))


async def test_the_tasks_are_opened_once_on_a_rerun(prev: PubWorld) -> None:
    from app.repositories.uow import tenant_uow
    from app.services.resolution.run import resolve

    made = await _previewed(
        prev,
        [_error("listing", ErrorCode.MARKETPLACE_UNKNOWN_ERROR, "77777", "Something unusual.")],
    )
    job_id = preview_jobs(prev, made.listing_id)[0]["id"]
    async with tenant_uow(prev.world.tenant("acme")) as uow:
        listing = await uow.listings.get(made.listing_id)
        job = await uow.job_runs.get_by_id(job_id)
        assert job is not None
        result = await resolve(uow, listing, entry="preview", job=job)
    assert result is not None and result.tasks.get("staff")
    count = prev.rows(
        "SELECT count(*) FROM human_tasks WHERE kind = 'marketplace_error_review' "
        "AND target_path = 'account.error_review' AND status = 'open' AND tenant_id = %s",
        [prev.world.tenant("acme")],
    )
    assert count[0][0] == 1
    assert len([r for r in preview_rows(prev, made.listing_id) if r["status"] == "open"]) == 1


async def test_another_tenants_listing_gets_no_task(api: httpx.AsyncClient, prev: PubWorld) -> None:
    made = await _previewed(
        prev,
        [_error("listing", ErrorCode.MARKETPLACE_UNKNOWN_ERROR, "77777", "Something unusual.")],
    )
    other = prev.rows(
        "SELECT count(*) FROM human_tasks WHERE tenant_id <> %s AND kind = "
        "'marketplace_error_review'",
        [prev.world.tenant("acme")],
    )
    assert other[0][0] == 0
    assert _tasks(prev, made.listing_id)


@pytest.mark.parametrize(
    ("field", "code", "raw"),
    [
        ("keywords.search_terms", ErrorCode.FIELD_TOO_LONG, "8560"),
        ("content.title", ErrorCode.BANNED_TERM, "8560"),
    ],
)
async def test_a_content_rejection_opens_the_staff_content_review(
    prev: PubWorld, field: str, code: ErrorCode, raw: str
) -> None:
    """Fix round 3 (R3-3), kept by T15/T16: a marketplace error on a keyword or content path that
    neither the content fix (no limit stated) nor the AI rewrite (AI paused) can take opens a
    staff task and links the issue, never a silent lane."""
    prev.fake.preview_errors = [_error(field, code, raw, "This value is not accepted here.")]
    made = await generated(prev.world)
    prev.conn.execute(
        "UPDATE ai_credit_state SET paused_reason = 'credit_exhausted', paused_at = now() "
        "WHERE id = 1"
    )
    try:
        for job in preview_jobs(prev, made.listing_id):
            if job["status"] == "queued":
                await run_job(job["id"])
    finally:
        prev.conn.execute(
            "UPDATE ai_credit_state SET paused_reason = NULL, paused_at = NULL WHERE id = 1"
        )
    tasks = _tasks(prev, made.listing_id)
    assert [(t["kind"], t["target_path"], t["field_path"]) for t in tasks] == [
        ("marketplace_error_review", "account.review.content_review", field)
    ]
    assert "text or keyword" in tasks[0]["question"]
