"""SI11 part B fix round 1 re-check (tester round 2), through the worker bodies, the reaper and
Postgres (the fake marketplace, the stub pipeline):
- a T15 content fix job whose base is no longer current hands its refusal to a person;
- an `ai_field` job whose worker died with no attempts left (the reaper's dead letter) does too;
- the platform's daily cap (admission rule 3) keeps the planner from an AI rewrite;
- the read labels a row a live `ai_field` job covers `ai_field`."""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
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
    "SELECT status, human_task_id FROM listing_issues WHERE listing_id = %s "
    "AND source = 'amazon_adapter' AND severity = 'error'"
)


def _kinds(pw: PubWorld, listing_id: uuid.UUID) -> set[str]:
    open_rows = [r for r in pw.rows(ADAPTER_ROWS, [listing_id]) if r[0] == "open"]
    assert open_rows, "no open adapter row"
    assert all(r[1] is not None for r in open_rows), open_rows
    return {str(pw.scalar("SELECT kind FROM human_tasks WHERE id = %s", [r[1]])) for r in open_rows}


async def test_a_content_fix_on_a_stale_base_hands_the_refusal_to_staff(
    api: httpx.AsyncClient, prev: PubWorld
) -> None:
    from mkos_schemas.codes import ErrorCode

    prev.fake.preview_errors = []
    made = await approved(prev)
    title = _content(prev, made.version_id)["content"]["title"]
    prev.fake.set_fail_fields(
        {
            "content.title": (
                "99301",
                f"The value for 'item_name' is longer than {len(title) - 3} characters.",
                ErrorCode.FIELD_TOO_LONG,
            )
        }
    )
    await publish_and_run(api, prev, made)
    (job,) = _gen_jobs(prev, made.listing_id, "auto_fix")
    payload = dict(job["payload_json"])
    payload["base_version_id"] = str(uuid.uuid4())  # the base is no longer the current version
    prev.conn.execute(
        "UPDATE job_runs SET payload_json = %s::jsonb WHERE id = %s",
        [json.dumps(payload), job["id"]],
    )
    prev.fake.set_fail_fields({})
    await run_job(job["id"])
    assert prev.job(job["id"])["status"] in ("failed", "dead_letter")
    row = prev.listing(made.listing_id)
    assert (row["status"], row["status_reason"]) == ("failed", "generation_failed")
    assert _kinds(prev, made.listing_id) == {"marketplace_error_review"}
    # the read of this listing: test_si11b_stale_pending_check.py


async def test_a_reaped_ai_field_job_hands_the_refusal_to_staff(
    api: httpx.AsyncClient, prev: PubWorld
) -> None:
    from app.workers.periodic import reap_once

    prev.fake.preview_errors = []
    made = await generated(prev.world, OWNER)
    prev.fake.preview_errors = [_banned()]
    await _run_previews(prev, made.listing_id)
    (job,) = _gen_jobs(prev, made.listing_id, "ai_field")
    # the worker claimed it on its last attempt and died: the lease ran out
    prev.conn.execute(
        "UPDATE job_runs SET status = 'running', attempt = max_attempts, started_at = now(), "
        "lease_expires_at = now() - interval '1 second' WHERE id = %s",
        [job["id"]],
    )
    reaped = await reap_once()
    assert prev.job(job["id"])["status"] == "dead_letter", reaped
    row = prev.listing(made.listing_id)
    assert (row["status"], row["status_reason"]) == ("failed", "generation_failed")
    assert _kinds(prev, made.listing_id) == {"marketplace_error_review"}
    body = await _read(api, prev, made.listing_id)
    assert body["state"] == "stopped" and body["next_step"]["kind"] == "regenerate", body


async def test_the_platform_daily_cap_plans_no_ai_rewrite(
    prev: PubWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    prev.fake.preview_errors = []
    made = await generated(prev.world, OWNER)
    monkeypatch.setattr("app.workers.budget.platform_cap", lambda: Decimal("0"))
    prev.fake.preview_errors = [_banned()]
    await _run_previews(prev, made.listing_id)
    assert _gen_jobs(prev, made.listing_id, "ai_field") == []
    assert prev.listing(made.listing_id)["status"] != "generating"
    assert _kinds(prev, made.listing_id) == {"marketplace_error_review"}


async def test_a_row_a_live_ai_field_job_covers_reads_ai_field(
    api: httpx.AsyncClient, prev: PubWorld
) -> None:
    prev.fake.preview_errors = []
    made = await generated(prev.world, OWNER)
    prev.fake.preview_errors = [_banned()]
    await _run_previews(prev, made.listing_id)
    (job,) = _gen_jobs(prev, made.listing_id, "ai_field")
    assert len(job["payload_json"]["target_fields"]) <= 10
    body = await _read(api, prev, made.listing_id)
    assert body["state"] == "fixing", body
    listed = await api.get(
        "/api/v1/issues",
        params={"listing_id": str(made.listing_id)},
        headers=prev.world.headers(REVIEWER),
    )
    assert listed.status_code == 200, listed.text
    lanes: list[Any] = [
        i["lane"] for i in listed.json()["data"] if i["field_path"] == "content.description"
    ]
    assert lanes and set(lanes) == {"ai_field"}, lanes
