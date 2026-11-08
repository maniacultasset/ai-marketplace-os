"""SI11 part B fix round 2 (B2-1; tester repro r6). A T15 content fix job on a refused
submission that ends with no version (base no longer current): the hand-over opens a staff task,
and the `remote_check_status = 'pending'` stamp request_preview wrote while the status sync job was
live must not hold the read at `checking` / `wait` forever. enqueue_pending skips the listing (it
was `generating`, then `failed`), so no preview job ever runs: the hook settles the stamp `failed`
and the read reaches the D-37 safety net (`stopped`, next step `regenerate`)."""

from __future__ import annotations

import json
import uuid

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .preview_support import prev, preview_jobs
from .publish_support import PubWorld, approved, publish_and_run, run_job
from .test_si11b_attacks import _content, _gen_jobs, _read, _run_previews

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["prev"]


async def test_a_failed_content_fix_on_a_submission_reads_stopped(
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
    payload["base_version_id"] = str(uuid.uuid4())
    prev.conn.execute(
        "UPDATE job_runs SET payload_json = %s::jsonb WHERE id = %s",
        [json.dumps(payload), job["id"]],
    )
    prev.fake.set_fail_fields({})
    await run_job(job["id"])
    await _run_previews(prev, made.listing_id)
    assert [
        j for j in preview_jobs(prev, made.listing_id) if j["status"] in ("queued", "running")
    ] == []
    row = prev.listing(made.listing_id)
    assert (row["status"], row["status_reason"]) == ("failed", "generation_failed")
    body = await _read(api, prev, made.listing_id)
    assert body["state"] == "stopped" and body["next_step"]["kind"] == "regenerate", (
        body["state"],
        body["remote_check"],
        body["next_step"],
    )
    assert body["remote_check"]["status"] == "failed", body["remote_check"]
    assert row["remote_check_status"] == "failed"
