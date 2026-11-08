"""SI11 part B, tester round 1: attacks on the content fix (T15), the seller's fact answer (T17),
the AI single-field rewrite (T16) and the resolution read (T20), through the API, the worker
bodies and Postgres. The marketplace is the in-process fake; the AI is the stub pipeline."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource, Severity
from mkos_schemas.errors import ErrorItem

from .conftest import LOCAL_HOSTS
from .preview_support import prev, preview_jobs
from .publish_support import PubWorld, approved, publish_and_run, run_job
from .review_support import (
    OPERATOR,
    OWNER,
    REVIEWER,
    Made,
    generated,
    item,
    post_approve,
)

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["prev"]


def _content(pw: PubWorld, version_id: Any) -> dict[str, Any]:
    raw = pw.scalar("SELECT content_json FROM listing_versions WHERE id = %s", [version_id])
    return raw if isinstance(raw, dict) else json.loads(raw)


def _gen_jobs(pw: PubWorld, listing_id: uuid.UUID, mode: str) -> list[dict[str, Any]]:
    cur = pw.conn.execute(
        "SELECT * FROM job_runs WHERE listing_id = %s AND kind = 'listing_generate' "
        "AND payload_json->>'mode' = %s ORDER BY queued_at",
        [listing_id, mode],
    )
    names = [d.name for d in cur.description or ()]
    return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


def _publish_jobs(pw: PubWorld, listing_id: uuid.UUID) -> int:
    return int(
        pw.scalar(
            "SELECT count(*) FROM job_runs WHERE listing_id = %s AND kind = 'listing_publish'",
            [listing_id],
        )
    )


async def _run_previews(pw: PubWorld, listing_id: uuid.UUID) -> None:
    for job in preview_jobs(pw, listing_id):
        if job["status"] == "queued":
            await run_job(job["id"])


def _too_long(limit: int, field: str = "content.title") -> ErrorItem:
    return ErrorItem(
        field=field,
        error_code=ErrorCode.FIELD_TOO_LONG,
        severity=Severity.ERROR,
        message=f"The value for 'item_name' is longer than {limit} characters.",
        source=ErrorSource.AMAZON_ADAPTER,
        retryable=False,
        raw_code="99301",
    )


async def _read(api: httpx.AsyncClient, pw: PubWorld, listing_id: Any) -> dict[str, Any]:
    response = await api.get(
        f"/api/v1/listings/{listing_id}/resolution", headers=pw.world.headers(REVIEWER)
    )
    assert response.status_code == 200, response.text
    return response.json()  # type: ignore[no-any-return]


# ---- T15 / AC9: a submission the marketplace refused on the title, then "Approve and resend" ----


async def test_a_refused_submission_title_is_fixed_then_only_a_person_resends(
    api: httpx.AsyncClient, prev: PubWorld
) -> None:
    prev.fake.preview_errors = []
    made = await approved(prev)  # maker OPERATOR, approved by REVIEWER
    title = _content(prev, made.version_id)["content"]["title"]
    limit = len(title) - 3
    prev.fake.set_fail_fields(
        {
            "content.title": (
                "99301",
                f"The value for 'item_name' is longer than {limit} characters.",
                ErrorCode.FIELD_TOO_LONG,
            )
        }
    )
    await publish_and_run(api, prev, made)
    row = prev.listing(made.listing_id)
    assert (row["status"], row["last_event"]) == ("generating", "auto_fix"), row
    (job,) = _gen_jobs(prev, made.listing_id, "auto_fix")
    assert _publish_jobs(prev, made.listing_id) == 1  # no resend of a content change
    assert (await _read(api, prev, made.listing_id))["state"] == "fixing"

    prev.fake.set_fail_fields({})
    await run_job(job["id"])
    assert prev.job(job["id"])["status"] == "succeeded", prev.job(job["id"])["error_report_json"]
    row = prev.listing(made.listing_id)
    fixed_id = row["current_version_id"]
    assert row["status"] == "needs_review" and row["approved_version_id"] is None
    assert len(_content(prev, fixed_id)["content"]["title"]) <= limit
    await _run_previews(prev, made.listing_id)
    pending = [j for j in preview_jobs(prev, made.listing_id) if j["status"] == "queued"]
    for j in pending:
        await run_job(j["id"])
    assert all(prev.job(j["id"])["status"] == "succeeded" for j in pending)
    assert _publish_jobs(prev, made.listing_id) == 1
    read = await _read(api, prev, made.listing_id)
    assert read["state"] == "fix_ready", read

    fixed = Made(made.listing_id, fixed_id, job["id"])
    own = await post_approve(api, prev.world, OPERATOR, [item(fixed)])  # the maker
    assert own.status_code == 403 or own.json()["items"][0]["http_status"] == 403, own.text
    other = await post_approve(api, prev.world, REVIEWER, [item(fixed)])
    assert other.json()["items"][0]["http_status"] == 200, other.text
    assert _publish_jobs(prev, made.listing_id) == 1  # the approval alone sends nothing
    await publish_and_run(api, prev, fixed)
    assert prev.listing(made.listing_id)["status"] == "published"
    assert (await _read(api, prev, made.listing_id))["state"] == "live"


# ---- T15: maker-checker carries an earlier editor through the fix version ----


async def test_the_editor_of_the_base_cannot_approve_the_fix_version(
    api: httpx.AsyncClient, prev: PubWorld
) -> None:
    prev.fake.preview_errors = []
    made = await generated(prev.world, OPERATOR)
    await _run_previews(prev, made.listing_id)
    base = await _edit_title(
        api, prev, made, OWNER, "Cotton Kurta Set with Dupatta for Women Pure Cotton"
    )
    title = _content(prev, base.version_id)["content"]["title"]
    prev.fake.preview_errors = [_too_long(len(title) - 3)]
    await _run_previews(prev, made.listing_id)
    jobs = _gen_jobs(prev, made.listing_id, "auto_fix")
    assert len(jobs) == 1, title
    (job,) = jobs
    prev.fake.preview_errors = []
    await run_job(job["id"])
    fixed_id = prev.listing(made.listing_id)["current_version_id"]
    assert fixed_id != base.version_id
    await _run_previews(prev, made.listing_id)
    fixed = Made(made.listing_id, fixed_id, job["id"])
    # OWNER edited the base: still a contributor of the fix version (the chain carries forward)
    refused = await post_approve(api, prev.world, OWNER, [item(fixed)])
    body = refused.json()["items"][0]
    assert body["http_status"] == 403, refused.text
    assert body["error"]["error_code"] == "MAKER_CHECKER_VIOLATION"
    other = await post_approve(api, prev.world, REVIEWER, [item(fixed)])
    assert other.json()["items"][0]["http_status"] == 200, other.text


async def _edit_title(
    api: httpx.AsyncClient, pw: PubWorld, made: Made, email: str, title: str
) -> Made:
    from .products_support import bearer_for

    headers = {**bearer_for(pw.world, email), "If-Match": f'"{made.version_id}"'}
    response = await api.patch(
        f"/api/v1/listings/{made.listing_id}/content",
        headers=headers,
        json={"content": {**_content(pw, made.version_id)["content"], "title": title}},
    )
    assert response.status_code == 200, response.text
    return Made(made.listing_id, uuid.UUID(response.json()["current_version_id"]), made.job_id)


# ---- T15: duplicate delivery of the auto-fix job writes one version ----


async def test_a_second_delivery_of_the_auto_fix_job_writes_no_second_version(
    prev: PubWorld,
) -> None:
    prev.fake.preview_errors = []
    made = await generated(prev.world)
    title = _content(prev, made.version_id)["content"]["title"]
    prev.fake.preview_errors = [_too_long(len(title) - 3)]
    await _run_previews(prev, made.listing_id)
    (job,) = _gen_jobs(prev, made.listing_id, "auto_fix")
    prev.fake.preview_errors = []
    await run_job(job["id"])
    versions = prev.scalar(
        "SELECT count(*) FROM listing_versions WHERE listing_id = %s", [made.listing_id]
    )
    await run_job(job["id"])  # a redelivered (already terminal) job
    assert (
        prev.scalar(
            "SELECT count(*) FROM listing_versions WHERE listing_id = %s", [made.listing_id]
        )
        == versions
    )
    assert prev.job(job["id"])["status"] == "succeeded"


# ---- T16: the credit runs out between planning and the claim ----


def _banned() -> ErrorItem:
    return ErrorItem(
        field="content.description",
        error_code=ErrorCode.BANNED_TERM,
        severity=Severity.ERROR,
        message="The description uses a term Amazon does not allow.",
        source=ErrorSource.AMAZON_ADAPTER,
        retryable=False,
        raw_code="8560",
    )


async def test_a_paused_ai_at_claim_makes_no_model_call_and_no_version(prev: PubWorld) -> None:
    prev.fake.preview_errors = []
    made = await generated(prev.world, OWNER)
    prev.fake.preview_errors = [_banned()]
    await _run_previews(prev, made.listing_id)
    (job,) = _gen_jobs(prev, made.listing_id, "ai_field")
    runs = prev.scalar("SELECT count(*) FROM agent_runs WHERE listing_id = %s", [made.listing_id])
    prev.conn.execute(
        "UPDATE ai_credit_state SET paused_reason = 'credit_exhausted', paused_at = now() "
        "WHERE id = 1"
    )
    try:
        await run_job(job["id"])
    finally:
        prev.conn.execute(
            "UPDATE ai_credit_state SET paused_reason = NULL, paused_at = NULL WHERE id = 1"
        )
    assert prev.job(job["id"])["status"] in ("failed", "dead_letter")
    assert (
        prev.scalar("SELECT count(*) FROM agent_runs WHERE listing_id = %s", [made.listing_id])
        == runs
    )
    row = prev.listing(made.listing_id)
    assert row["current_version_id"] == made.version_id
    assert _publish_jobs(prev, made.listing_id) == 0


# ---- T17: replays and conflicting answers ----


async def test_a_different_answer_after_the_fact_version_is_refused(
    api: httpx.AsyncClient, prev: PubWorld
) -> None:
    from .test_resolution_seller import _answer, _asked, _task_id

    made = await _asked(prev, "facts.gender", clear="gender")
    task_id = _task_id(prev, made.listing_id, "facts.gender")
    first = await _answer(api, prev, task_id, "women", OPERATOR, str(uuid.uuid4()))
    assert first.status_code == 200, first.text
    version = prev.listing(made.listing_id)["current_version_id"]
    # the same answer under a new key: no second version
    again = await _answer(api, prev, task_id, "women", OPERATOR, str(uuid.uuid4()))
    assert again.status_code == 200, again.text
    # a different answer: refused, the version stays
    other = await _answer(api, prev, task_id, "men", OWNER, str(uuid.uuid4()))
    assert other.status_code == 409, other.text
    assert prev.listing(made.listing_id)["current_version_id"] == version
    assert (
        prev.scalar(
            "SELECT gender FROM products p JOIN listings l ON l.product_id = p.id "
            "WHERE l.id = %s",
            [made.listing_id],
        )
        == "women"
    )


async def test_a_brand_answer_names_only_the_tenants_own_brand(
    api: httpx.AsyncClient, prev: PubWorld
) -> None:
    from .test_resolution_seller import _answer, _asked, _task_id

    made = await _asked(prev, "facts.brand_name", clear="brand_name")
    task_id = _task_id(prev, made.listing_id, "facts.brand_name")
    if task_id is None:
        pytest.skip("no brand question opened for this world (brand fact derivable)")
    bad = await _answer(api, prev, task_id, "Some Other Brand 9z", OPERATOR, str(uuid.uuid4()))
    assert bad.status_code == 422, bad.text
    assert prev.listing(made.listing_id)["current_version_id"] == made.version_id


# ---- T20: the inbox labels never cross tenants ----


async def test_the_inbox_lane_and_the_read_never_show_another_tenants_rows(
    api: httpx.AsyncClient, prev: PubWorld
) -> None:
    prev.fake.preview_errors = [_too_long(5)]
    made = await generated(prev.world)
    await _run_previews(prev, made.listing_id)
    other = await api.get(
        f"/api/v1/listings/{made.listing_id}/resolution",
        headers=prev.world.headers("owner@bharat.test"),
    )
    assert other.status_code == 404, other.text
    page = await api.get("/api/v1/issues", headers=prev.world.headers("owner@bharat.test"))
    assert page.status_code == 200, page.text
    ids = {i["listing_id"] for i in page.json()["data"]}
    assert str(made.listing_id) not in ids


async def test_publish_of_an_unapproved_fix_version_is_refused(
    api: httpx.AsyncClient, prev: PubWorld
) -> None:
    prev.fake.preview_errors = []
    made = await generated(prev.world)
    title = _content(prev, made.version_id)["content"]["title"]
    prev.fake.preview_errors = [_too_long(len(title) - 3)]
    await _run_previews(prev, made.listing_id)
    (job,) = _gen_jobs(prev, made.listing_id, "auto_fix")
    prev.fake.preview_errors = []
    await run_job(job["id"])
    await _run_previews(prev, made.listing_id)
    item_out = await publish_one_status(api, prev, made)
    assert item_out != 202
    assert _publish_jobs(prev, made.listing_id) == 0


async def publish_one_status(api: httpx.AsyncClient, pw: PubWorld, made: Made) -> int:
    from .publish_support import post_publish

    response = await post_publish(api, pw, OPERATOR, [{"listing_id": str(made.listing_id)}])
    if response.status_code != 202:
        return int(response.status_code)
    return int(response.json()["items"][0]["http_status"])
