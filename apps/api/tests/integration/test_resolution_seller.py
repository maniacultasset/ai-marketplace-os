"""SI11 T17 answer half (AC11; P:B6, T51): the seller's answer to the question behind a
marketplace refusal writes the fact, makes a fact-only `edit` version whose editor is the
answerer (so the answerer never approves it), revalidates it in the same request and asks the
marketplace's dry run again. Nothing resends on its own."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from mkos_schemas.codes import ErrorCode

from .conftest import LOCAL_HOSTS
from .preview_support import prev, preview_jobs
from .publish_support import PubWorld
from .review_support import OWNER, REVIEWER, Made, generated, item, post_approve
from .test_resolution_lanes import _error, _tasks

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["prev"]


async def _run_previews(pw: PubWorld, listing_id: uuid.UUID) -> None:
    from .publish_support import run_job

    for job in preview_jobs(pw, listing_id):
        if job["status"] == "queued":
            await run_job(job["id"])


async def _asked(pw: PubWorld, field: str, *, clear: str | None = None) -> Made:
    """A listing whose dry run refuses `field` as missing; `clear` blanks a product column
    first, so no repair can fill it and the seller is asked."""
    pw.fake.preview_errors = []
    made = await generated(pw.world)
    if clear is not None:
        product = pw.scalar("SELECT product_id FROM listings WHERE id = %s", [made.listing_id])
        pw.conn.execute(
            f"UPDATE products SET {clear} = NULL WHERE id = %s",  # noqa: S608 - a test column
            [product],
        )
    pw.fake.preview_errors = [
        _error(field, ErrorCode.REQUIRED_FIELD_MISSING, "90220", f"'{field}' is required.")
    ]
    await _run_previews(pw, made.listing_id)
    pw.fake.preview_errors = []
    return made


async def _answer(
    api: httpx.AsyncClient, pw: PubWorld, task_id: Any, value: str, email: str, key: str
) -> httpx.Response:
    headers = {**pw.world.headers(email), "Idempotency-Key": key}
    return await api.post(f"/api/v1/tasks/{task_id}/answer", headers=headers, json={"value": value})


def _task_id(pw: PubWorld, listing_id: uuid.UUID, path: str) -> Any:
    return pw.scalar(
        "SELECT t.id FROM human_tasks t JOIN listing_issues i ON i.human_task_id = t.id "
        "WHERE i.listing_id = %s AND t.target_path = %s AND t.status = 'open'",
        [listing_id, path],
    )


async def test_a_gender_answer_makes_a_fact_version_the_answerer_cannot_approve(
    api: httpx.AsyncClient, prev: PubWorld
) -> None:
    made = await _asked(prev, "facts.gender", clear="gender")
    tasks = _tasks(prev, made.listing_id)
    assert [(t["kind"], t["target_path"]) for t in tasks] == [("missing_fact", "facts.gender")]
    task_id = _task_id(prev, made.listing_id, "facts.gender")
    before = len(preview_jobs(prev, made.listing_id))
    key = str(uuid.uuid4())

    response = await _answer(api, prev, task_id, "women", OWNER, key)
    assert response.status_code == 200, response.text
    product = prev.rows(
        "SELECT p.gender, p.provenance->'gender'->>'captured_via', "
        "p.provenance->'gender'->>'evidence_ref' FROM products p JOIN listings l "
        "ON l.product_id = p.id WHERE l.id = %s",
        [made.listing_id],
    )[0]
    assert product == ("women", "human_task", f"human_task:{task_id}")
    row = prev.listing(made.listing_id)
    fact_version = row["current_version_id"]
    assert fact_version != made.version_id
    assert row["status"] in ("validated", "needs_review"), (
        row["status"],
        row["status_reason"],
        row["last_event"],
        prev.rows(
            "SELECT source, field_path, error_code, status, listing_version_id = %s FROM listing_issues WHERE listing_id = %s ORDER BY created_at",
            [fact_version, made.listing_id],
        ),
    )
    assert row["last_event"] == "edit"
    version = prev.rows(
        "SELECT origin, changed_paths, edited_by_user_id, parent_version_id "
        "FROM listing_versions WHERE id = %s",
        [fact_version],
    )[0]
    assert version == ("edit", ["facts.gender"], prev.world.user(OWNER), made.version_id)
    assert len(preview_jobs(prev, made.listing_id)) == before + 1

    # the replay makes nothing more
    again = await _answer(api, prev, task_id, "women", OWNER, key)
    assert again.status_code == 200
    assert prev.listing(made.listing_id)["current_version_id"] == fact_version
    assert len(preview_jobs(prev, made.listing_id)) == before + 1

    await _run_previews(prev, made.listing_id)
    fixed = Made(made.listing_id, fact_version, made.job_id)
    own = await post_approve(api, prev.world, OWNER, [item(fixed)])
    assert own.json()["items"][0]["http_status"] == 403, own.text
    assert own.json()["items"][0]["error"]["error_code"] == "MAKER_CHECKER_VIOLATION"
    other = await post_approve(api, prev.world, REVIEWER, [item(fixed)])
    assert other.json()["items"][0]["http_status"] == 200, other.text
    assert (
        prev.scalar(
            "SELECT count(*) FROM job_runs WHERE listing_id = %s AND kind = 'listing_publish'",
            [made.listing_id],
        )
        == 0
    )


async def test_a_gender_answer_outside_the_vocabulary_is_refused(
    api: httpx.AsyncClient, prev: PubWorld
) -> None:
    made = await _asked(prev, "facts.gender", clear="gender")
    task_id = _task_id(prev, made.listing_id, "facts.gender")
    response = await _answer(api, prev, task_id, "aliens", OWNER, str(uuid.uuid4()))
    assert response.status_code == 422
    assert prev.listing(made.listing_id)["current_version_id"] == made.version_id


async def test_an_attribute_answer_behind_a_refusal_also_makes_the_fact_version(
    api: httpx.AsyncClient, prev: PubWorld
) -> None:
    made = await _asked(prev, "attributes.color")
    task_id = _task_id(prev, made.listing_id, "facts.attributes.color")
    assert task_id is not None
    response = await _answer(api, prev, task_id, "Red", OWNER, str(uuid.uuid4()))
    assert response.status_code == 200, response.text
    version = prev.rows(
        "SELECT origin, changed_paths FROM listing_versions WHERE id = %s",
        [prev.listing(made.listing_id)["current_version_id"]],
    )[0]
    assert version == ("edit", ["facts.attributes.color"])


async def test_an_attribute_answer_version_is_checked_on_the_answered_facts(
    api: httpx.AsyncClient, prev: PubWorld
) -> None:
    """QA: the check of the answer's version read the facts from before the answer, so its pins
    went stale and Approve and resend was a 409 STALE_VERSION until a manual re-check."""
    made = await _asked(prev, "attributes.color")
    task_id = _task_id(prev, made.listing_id, "facts.attributes.color")
    response = await _answer(api, prev, task_id, "Red", OWNER, str(uuid.uuid4()))
    assert response.status_code == 200, response.text
    fact_version = prev.listing(made.listing_id)["current_version_id"]
    assert fact_version != made.version_id
    await _run_previews(prev, made.listing_id)
    fixed = Made(made.listing_id, fact_version, made.job_id)
    other = await post_approve(api, prev.world, REVIEWER, [item(fixed)])
    assert other.json()["items"][0]["http_status"] == 200, other.text


async def test_a_question_behind_an_open_error_reads_blocking(
    api: httpx.AsyncClient, prev: PubWorld
) -> None:
    """QA: the inbox said "This doesn't block your listing" for a question an open listing
    ERROR waits on; the list read now says so."""
    made = await _asked(prev, "attributes.color")
    task_id = _task_id(prev, made.listing_id, "facts.attributes.color")
    headers = prev.world.headers(OWNER)
    listed = (await api.get("/api/v1/tasks", headers=headers)).json()["data"]
    assert {r["id"]: r["blocking"] for r in listed}[str(task_id)] is True
    response = await _answer(api, prev, task_id, "Red", OWNER, str(uuid.uuid4()))
    assert response.status_code == 200, response.text
    assert response.json()["blocking"] is False
    applied = (await api.get("/api/v1/tasks", headers=headers, params={"status": "applied"})).json()
    assert {r["id"]: r["blocking"] for r in applied["data"]}[str(task_id)] is False


async def test_a_second_open_question_waits_for_both_answers(
    api: httpx.AsyncClient, prev: PubWorld
) -> None:
    prev.fake.preview_errors = []
    made = await generated(prev.world)
    product = prev.scalar("SELECT product_id FROM listings WHERE id = %s", [made.listing_id])
    prev.conn.execute("UPDATE products SET gender = NULL WHERE id = %s", [product])
    prev.fake.preview_errors = [
        _error("facts.gender", ErrorCode.REQUIRED_FIELD_MISSING, "90220", "gender is required."),
        _error("attributes.color", ErrorCode.REQUIRED_FIELD_MISSING, "90220", "color is required."),
    ]
    await _run_previews(prev, made.listing_id)
    prev.fake.preview_errors = []
    gender = _task_id(prev, made.listing_id, "facts.gender")
    color = _task_id(prev, made.listing_id, "facts.attributes.color")
    assert gender is not None and color is not None
    assert (await _answer(api, prev, gender, "men", OWNER, str(uuid.uuid4()))).status_code == 200
    assert prev.listing(made.listing_id)["current_version_id"] == made.version_id
    assert (await _answer(api, prev, color, "Blue", OWNER, str(uuid.uuid4()))).status_code == 200
    version = prev.rows(
        "SELECT origin, changed_paths FROM listing_versions WHERE id = %s",
        [prev.listing(made.listing_id)["current_version_id"]],
    )[0]
    assert version == ("edit", ["facts.attributes.color", "facts.gender"])


async def test_another_tenants_task_is_404(api: httpx.AsyncClient, prev: PubWorld) -> None:
    made = await _asked(prev, "facts.gender", clear="gender")
    task_id = _task_id(prev, made.listing_id, "facts.gender")
    headers = {**prev.world.headers("owner@bharat.test"), "Idempotency-Key": str(uuid.uuid4())}
    response = await api.post(
        f"/api/v1/tasks/{task_id}/answer", headers=headers, json={"value": "men"}
    )
    assert response.status_code == 404
