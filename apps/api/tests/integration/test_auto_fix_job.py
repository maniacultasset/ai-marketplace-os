"""SI11 T15 (AC9; P:B5 part 1, D3): the deterministic content auto-fix. A marketplace length
refusal on the title makes a `listing_generate` job (`mode auto_fix`); the job writes a
`regeneration` version with only that path changed, whose maker is the base version's
generator, so only another person can approve it. Nothing resends on its own."""

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
from .publish_support import PubWorld, run_job
from .review_support import OWNER, REVIEWER, Made, generated, item, post_approve

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["prev"]


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


def _content(pw: PubWorld, version_id: Any) -> dict[str, Any]:
    raw = pw.scalar("SELECT content_json FROM listing_versions WHERE id = %s", [version_id])
    return raw if isinstance(raw, dict) else json.loads(raw)


def _jobs(pw: PubWorld, listing_id: uuid.UUID, mode: str = "auto_fix") -> list[dict[str, Any]]:
    cur = pw.conn.execute(
        "SELECT * FROM job_runs WHERE listing_id = %s AND kind = 'listing_generate' "
        "AND payload_json->>'mode' = %s ORDER BY queued_at",
        [listing_id, mode],
    )
    names = [d.name for d in cur.description or ()]
    return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


async def _run_previews(pw: PubWorld, listing_id: uuid.UUID) -> None:
    for job in preview_jobs(pw, listing_id):
        if job["status"] == "queued":
            await run_job(job["id"])


async def _refused(pw: PubWorld, *, cut: int = 3, email: str = OWNER) -> tuple[Made, int]:
    """A listing whose title the dry run refuses as `cut` characters too long."""
    pw.fake.preview_errors = []
    made = await generated(pw.world, email)
    title = _content(pw, made.version_id)["content"]["title"]
    limit = len(title) - cut
    pw.fake.preview_errors = [_too_long(limit)]
    await _run_previews(pw, made.listing_id)
    return made, limit


async def test_a_title_refusal_makes_a_fix_version_only_another_person_approves(
    api: httpx.AsyncClient, prev: PubWorld
) -> None:
    made, limit = await _refused(prev)
    (job,) = _jobs(prev, made.listing_id)
    assert job["status"] == "queued"
    assert job["payload_json"]["trigger"] == "resolution"
    assert job["payload_json"]["base_version_id"] == str(made.version_id)
    assert job["created_by"] == prev.world.user(OWNER)
    row = prev.listing(made.listing_id)
    assert (row["status"], row["last_event"]) == ("generating", "auto_fix")

    prev.fake.preview_errors = []
    await run_job(job["id"])
    assert prev.job(job["id"])["status"] == "succeeded"
    row = prev.listing(made.listing_id)
    fixed_id = row["current_version_id"]
    assert fixed_id != made.version_id
    assert row["status"] == "needs_review" and row["approved_version_id"] is None
    version = prev.rows(
        "SELECT origin, parent_version_id, changed_paths, generated_by_user_id, edited_by_user_id "
        "FROM listing_versions WHERE id = %s",
        [fixed_id],
    )[0]
    assert version == (
        "regeneration",
        made.version_id,
        ["content.title"],
        prev.world.user(OWNER),
        None,
    )
    base, fixed = _content(prev, made.version_id), _content(prev, fixed_id)
    assert len(fixed["content"]["title"]) <= limit
    assert base["content"]["title"].startswith(fixed["content"]["title"])
    # only the title changed (the version stamps aside)
    for body in (base, fixed):
        body["content"].pop("title")
        for stamp in ("version", "status"):
            body.pop(stamp, None)
    assert fixed == base
    # the refusal is closed by the fix; no resend, no publish job
    assert (
        prev.scalar(
            "SELECT count(*) FROM listing_issues WHERE listing_id = %s AND status = 'open' "
            "AND source = 'amazon_adapter' AND severity = 'error'",
            [made.listing_id],
        )
        == 0
    )
    assert (
        prev.scalar(
            "SELECT resolution FROM listing_issues WHERE listing_id = %s AND field_path = "
            "'content.title' AND source = 'amazon_adapter'",
            [made.listing_id],
        )
        == "deterministic_fix"
    )
    assert (
        prev.scalar(
            "SELECT count(*) FROM job_runs WHERE listing_id = %s AND kind = 'listing_publish'",
            [made.listing_id],
        )
        == 0
    )
    # the fix version is checked with the marketplace again (a resolution re-check)
    await _run_previews(prev, made.listing_id)
    assert prev.listing(made.listing_id)["remote_check_status"] == "clean"

    # the read says a fix waits for a teammate
    body = (
        await api.get(
            f"/api/v1/listings/{made.listing_id}/resolution",
            headers=prev.world.headers(REVIEWER),
        )
    ).json()
    assert body["state"] == "fix_ready" and body["next_step"]["kind"] == "approve"

    fixed_made = Made(made.listing_id, fixed_id, job["id"])
    own = await post_approve(api, prev.world, OWNER, [item(fixed_made)])
    assert own.json()["items"][0]["http_status"] == 403, own.text
    assert own.json()["items"][0]["error"]["error_code"] == "MAKER_CHECKER_VIOLATION"
    other = await post_approve(api, prev.world, REVIEWER, [item(fixed_made)])
    assert other.json()["items"][0]["http_status"] == 200, other.text


async def test_a_refusal_the_trim_cannot_meet_opens_a_staff_review(prev: PubWorld) -> None:
    made, _limit = await _refused(prev, cut=10_000)  # a limit below zero: no fix
    assert _jobs(prev, made.listing_id) == []
    task = prev.rows(
        "SELECT t.kind, t.target_path FROM listing_issues i JOIN human_tasks t "
        "ON t.id = i.human_task_id WHERE i.listing_id = %s AND i.status = 'open'",
        [made.listing_id],
    )
    assert task == [("marketplace_error_review", "account.review.content_review")]


async def test_the_auto_fix_key_enqueues_once(prev: PubWorld) -> None:
    from app.repositories.uow import tenant_uow
    from app.services.resolution.run import resolve

    made, _limit = await _refused(prev)
    (job,) = _jobs(prev, made.listing_id)
    preview = preview_jobs(prev, made.listing_id)[-1]
    async with tenant_uow(prev.world.tenant("acme")) as uow:
        listing = await uow.listings.get(made.listing_id)
        claimed = await uow.job_runs.get_by_id(preview["id"])
        assert claimed is not None
        await resolve(uow, listing, entry="preview", job=claimed)
    assert [j["id"] for j in _jobs(prev, made.listing_id)] == [job["id"]]


async def test_a_job_whose_base_is_no_longer_current_writes_no_version(prev: PubWorld) -> None:
    made, _limit = await _refused(prev)
    (job,) = _jobs(prev, made.listing_id)
    prev.conn.execute(
        "UPDATE job_runs SET payload_json = jsonb_set(payload_json, '{base_version_id}', %s) "
        "WHERE id = %s",
        [json.dumps(str(uuid.uuid4())), job["id"]],
    )
    await run_job(job["id"])
    assert prev.job(job["id"])["status"] == "failed"
    row = prev.listing(made.listing_id)
    assert row["current_version_id"] == made.version_id
    assert (row["status"], row["status_reason"]) == ("failed", "generation_failed")


async def test_an_auto_fix_move_without_its_job_is_refused(prev: PubWorld) -> None:
    import psycopg

    made = await generated(prev.world)
    with pytest.raises(psycopg.Error):
        prev.conn.execute(
            "UPDATE listings SET status = 'generating', last_event = 'auto_fix' WHERE id = %s",
            [made.listing_id],
        )


# ---- T16: the AI rewrite of the refused field only ----


def _banned(field: str = "content.description") -> ErrorItem:
    return ErrorItem(
        field=field,
        error_code=ErrorCode.BANNED_TERM,
        severity=Severity.ERROR,
        message="The description uses a term Amazon does not allow.",
        source=ErrorSource.AMAZON_ADAPTER,
        retryable=False,
        raw_code="8560",
    )


async def test_an_ai_rewrite_changes_only_the_refused_field(
    prev: PubWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.workers import generation

    seen: list[Any] = []
    real = getattr(generation, "run_listing_pipeline")  # noqa: B009 - the module re-exports none

    async def recording(ctx: Any, deps: Any) -> Any:
        seen.append(ctx)
        result = await real(ctx, deps)
        if ctx.target_fields and result.listing is not None:
            # the model rewrites the title too: the merge must keep only the description
            content = result.listing.content.model_copy(
                update={"title": "A different title", "description": "A clean new description."}
            )
            result = result.model_copy(
                update={"listing": result.listing.model_copy(update={"content": content})}
            )
        return result

    monkeypatch.setattr(generation, "run_listing_pipeline", recording)
    prev.fake.preview_errors = []
    made = await generated(prev.world, OWNER)
    runs_before = prev.scalar(
        "SELECT count(*) FROM agent_runs WHERE listing_id = %s", [made.listing_id]
    )
    prev.fake.preview_errors = [_banned()]
    await _run_previews(prev, made.listing_id)
    (job,) = _jobs(prev, made.listing_id, "ai_field")
    assert job["payload_json"]["target_fields"] == ["content.description"]
    (line,) = job["payload_json"]["feedback"]
    assert line.startswith("Amazon refused") and line.endswith("Rewrite only this field.")
    assert len(line) <= 500

    prev.fake.preview_errors = []
    await run_job(job["id"])
    assert prev.job(job["id"])["status"] == "succeeded", prev.job(job["id"])["error_report_json"]
    ctx = seen[-1]
    assert ctx.target_fields == ["content.description"] and ctx.review_feedback[0] == line
    row = prev.listing(made.listing_id)
    fixed_id = row["current_version_id"]
    assert row["status"] == "needs_review"
    version = prev.rows(
        "SELECT origin, changed_paths, generated_by_user_id FROM listing_versions WHERE id = %s",
        [fixed_id],
    )[0]
    assert version == ("regeneration", ["content.description"], prev.world.user(OWNER))
    base, fixed = _content(prev, made.version_id), _content(prev, fixed_id)
    assert fixed["content"]["description"] == "A clean new description."
    assert fixed["content"]["title"] == base["content"]["title"]
    # every model call of the job is recorded (invariant 7)
    runs = prev.scalar(
        "SELECT count(*) FROM agent_runs WHERE listing_id = %s AND job_id = %s",
        [made.listing_id, job["id"]],
    )
    assert runs >= 1
    assert (
        prev.scalar("SELECT count(*) FROM agent_runs WHERE listing_id = %s", [made.listing_id])
        == runs_before + runs
    )
    assert (
        prev.scalar(
            "SELECT resolution FROM listing_issues WHERE listing_id = %s AND source = 'amazon_adapter' "
            "AND field_path = 'content.description'",
            [made.listing_id],
        )
        == "ai_correction"
    )
    assert (
        prev.scalar(
            "SELECT count(*) FROM job_runs WHERE listing_id = %s AND kind = 'listing_publish'",
            [made.listing_id],
        )
        == 0
    )


async def test_paused_ai_sends_the_refusal_to_a_staff_review(prev: PubWorld) -> None:
    prev.fake.preview_errors = [_banned()]
    made = await generated(prev.world, OWNER)
    prev.conn.execute(
        "UPDATE ai_credit_state SET paused_reason = 'credit_exhausted', paused_at = now() WHERE id = 1"
    )
    try:
        await _run_previews(prev, made.listing_id)
    finally:
        prev.conn.execute(
            "UPDATE ai_credit_state SET paused_reason = NULL, paused_at = NULL WHERE id = 1"
        )
    assert _jobs(prev, made.listing_id, "ai_field") == []
    task = prev.rows(
        "SELECT t.target_path FROM listing_issues i JOIN human_tasks t ON t.id = i.human_task_id "
        "WHERE i.listing_id = %s AND i.status = 'open'",
        [made.listing_id],
    )
    assert task == [("account.review.content_review",)]
