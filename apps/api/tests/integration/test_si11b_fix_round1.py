"""SI11 part B fix round 1, through the API, the worker bodies and Postgres (the fake marketplace,
the stub pipeline):
- B1-1 (tester repro `test_r5_ai_refused_at_claim_stalls`): a fix job that ended with no version
  hands its still-open refusal to a person, and the read names a next step;
- B1-5: a tenant over its daily AI cap is never planned an AI rewrite (staff looks instead);
- B1-6 (backstop): an `ai_field` job closes only the refusals on the paths it was sent;
- B1-4: a row Kuinn is fixing, or fixed, reads `auto_content` in the inbox and the timeline."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .preview_support import prev
from .publish_support import PubWorld, approved, publish_and_run, run_job
from .review_support import OWNER, REVIEWER, generated
from .test_si11b_attacks import _banned, _content, _gen_jobs, _read, _run_previews

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["prev"]

ADAPTER_ROWS = (
    "SELECT status, human_task_id, resolution FROM listing_issues WHERE listing_id = %s "
    "AND source = 'amazon_adapter' AND severity = 'error'"
)


def _pause(pw: PubWorld) -> None:
    pw.conn.execute(
        "UPDATE ai_credit_state SET paused_reason = 'credit_exhausted', paused_at = now() "
        "WHERE id = 1"
    )


def _unpause(pw: PubWorld) -> None:
    pw.conn.execute(
        "UPDATE ai_credit_state SET paused_reason = NULL, paused_at = NULL WHERE id = 1"
    )


def _task_kind(pw: PubWorld, task_id: Any) -> str:
    return str(pw.scalar("SELECT kind FROM human_tasks WHERE id = %s", [task_id]))


async def test_a_refused_ai_claim_leaves_someone_a_next_step(
    api: httpx.AsyncClient, prev: PubWorld
) -> None:
    prev.fake.preview_errors = []
    made = await generated(prev.world, OWNER)
    prev.fake.preview_errors = [_banned()]
    await _run_previews(prev, made.listing_id)
    (job,) = _gen_jobs(prev, made.listing_id, "ai_field")
    _pause(prev)
    try:
        await run_job(job["id"])
    finally:
        _unpause(prev)
    row = prev.listing(made.listing_id)
    assert (row["status"], row["status_reason"]) == ("failed", "generation_failed")
    open_rows = [r for r in prev.rows(ADAPTER_ROWS, [made.listing_id]) if r[0] == "open"]
    assert open_rows
    # the content refusal nobody can fix by code is a staff content review (D-27)
    assert all(r[1] is not None for r in open_rows), open_rows
    assert {_task_kind(prev, r[1]) for r in open_rows} == {"marketplace_error_review"}
    body = await _read(api, prev, made.listing_id)
    assert body["state"] == "stopped" and body["next_step"]["kind"] == "regenerate", body


async def test_a_tenant_over_its_daily_cap_gets_no_ai_rewrite(prev: PubWorld) -> None:
    prev.fake.preview_errors = []
    made = await generated(prev.world, OWNER)
    tenant = prev.scalar("SELECT tenant_id FROM listings WHERE id = %s", [made.listing_id])
    before = prev.listing(made.listing_id)["status"]
    prev.conn.execute("UPDATE tenants SET llm_daily_budget_usd = 0 WHERE id = %s", [tenant])
    try:
        prev.fake.preview_errors = [_banned()]
        await _run_previews(prev, made.listing_id)
    finally:
        prev.conn.execute("UPDATE tenants SET llm_daily_budget_usd = NULL WHERE id = %s", [tenant])
    assert _gen_jobs(prev, made.listing_id, "ai_field") == []
    row = prev.listing(made.listing_id)
    assert row["status"] != "generating" and row["status"] in (before, "blocked"), row
    open_rows = [r for r in prev.rows(ADAPTER_ROWS, [made.listing_id]) if r[0] == "open"]
    assert open_rows and all(r[1] is not None for r in open_rows), open_rows
    assert {_task_kind(prev, r[1]) for r in open_rows} == {"marketplace_error_review"}


async def test_an_ai_rewrite_closes_only_the_refusals_on_its_targets(prev: PubWorld) -> None:
    prev.fake.preview_errors = []
    made = await generated(prev.world, OWNER)
    prev.fake.preview_errors = [_banned()]  # on content.description
    await _run_previews(prev, made.listing_id)
    (job,) = _gen_jobs(prev, made.listing_id, "ai_field")
    payload = dict(job["payload_json"])
    payload["target_fields"] = ["content.title"]  # a job whose targets miss the refused path
    prev.conn.execute(
        "UPDATE job_runs SET payload_json = %s::jsonb WHERE id = %s",
        [json.dumps(payload), job["id"]],
    )
    prev.fake.preview_errors = []
    await run_job(job["id"])
    assert prev.job(job["id"])["status"] == "succeeded", prev.job(job["id"])["error_report_json"]
    rows = prev.rows(
        "SELECT status, resolution FROM listing_issues WHERE id = ANY(%s::uuid[])",
        [list(payload["issue_ids"])],
    )
    assert rows and all(r[1] != "ai_correction" for r in rows), rows


async def test_a_row_kuinn_is_fixing_then_fixed_reads_auto_content(
    api: httpx.AsyncClient, prev: PubWorld
) -> None:
    from mkos_schemas.codes import ErrorCode

    prev.fake.preview_errors = []
    made = await approved(prev)
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
    (job,) = _gen_jobs(prev, made.listing_id, "auto_fix")
    headers = prev.world.headers(REVIEWER)

    async def lanes() -> tuple[list[Any], list[Any]]:
        listed = await api.get(
            "/api/v1/issues", params={"listing_id": str(made.listing_id)}, headers=headers
        )
        assert listed.status_code == 200, listed.text
        inbox = [i["lane"] for i in listed.json()["data"] if i["field_path"] == "content.title"]
        read = await _read(api, prev, made.listing_id)
        timeline = [
            f["lane"]
            for a in read["attempts"]
            for f in a["failed"]
            if f["field"] == "content.title"
        ]
        return inbox, timeline

    assert (await _read(api, prev, made.listing_id))["state"] == "fixing"
    inbox, timeline = await lanes()
    assert inbox and set(inbox) == {"auto_content"}, inbox
    assert timeline and set(timeline) == {"auto_content"}, timeline

    prev.fake.set_fail_fields({})
    await run_job(job["id"])
    await _run_previews(prev, made.listing_id)
    await _run_previews(prev, made.listing_id)  # the dry run's own follow-up
    assert (await _read(api, prev, made.listing_id))["state"] == "fix_ready"
    fixed = await api.get(
        "/api/v1/issues",
        params={"listing_id": str(made.listing_id), "status": "auto_fixed"},
        headers=headers,
    )
    assert fixed.status_code == 200, fixed.text
    shown = [i for i in fixed.json()["data"] if i["field_path"] == "content.title"]
    assert shown and {i["lane"] for i in shown} == {"auto_content"}, shown
    _inbox, timeline = await lanes()
    assert timeline and set(timeline) == {"auto_content"}, timeline
