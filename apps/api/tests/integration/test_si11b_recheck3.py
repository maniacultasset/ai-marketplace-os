"""SI11 part B re-check round 3 (B2-1, D-38). The orphaned `pending` dry-run stamp is settled
`failed` in `remote_preview.enqueue_pending` and the read counts `pending` as `checking` only while
a `listing_status_sync` job is live. These attack the fix from the sides the fix round did not:
the settle must never touch a real wait or a verdict, the normal (succeeding) content fix on a
refused submission must still end with the fix version checked `clean`, and the read's
`regenerate` next step after the failed fix must really lead back to a checked version."""

from __future__ import annotations

import json
import uuid

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .preview_support import prev, preview_jobs
from .products_support import bearer_for
from .publish_support import PubWorld, approved, publish_and_run, run_job
from .review_support import OPERATOR, generated
from .test_si11b_attacks import _content, _gen_jobs, _read, _run_previews

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["prev"]


def _check(pw: PubWorld, listing_id: uuid.UUID) -> tuple[str | None, uuid.UUID | None]:
    row = pw.listing(listing_id)
    return row["remote_check_status"], row["remote_check_version_id"]


async def _drain_previews(pw: PubWorld, listing_id: uuid.UUID) -> None:
    """Run queued dry runs until none is left (a terminal may enqueue the next one)."""
    for _ in range(5):
        queued = [j for j in preview_jobs(pw, listing_id) if j["status"] == "queued"]
        if not queued:
            return
        for job in queued:
            await run_job(job["id"])
    raise AssertionError("dry runs keep enqueuing")


async def _settle(pw: PubWorld, listing_id: uuid.UUID) -> None:
    from app.repositories.uow import tenant_uow
    from app.services.remote_preview import settle_orphan_check

    async with tenant_uow(pw.world.tenant("acme")) as uow:
        await settle_orphan_check(uow, await uow.listings.get(listing_id))


async def test_the_settle_leaves_a_real_wait_and_a_verdict_alone(
    api: httpx.AsyncClient, prev: PubWorld
) -> None:
    prev.fake.preview_errors = []
    made = await generated(prev.world)
    live = [j for j in preview_jobs(prev, made.listing_id) if j["status"] == "queued"]
    assert len(live) == 1
    assert _check(prev, made.listing_id) == ("pending", made.version_id)
    # a pending stamp with its preview job still queued is a real wait
    await _settle(prev, made.listing_id)
    assert _check(prev, made.listing_id) == ("pending", made.version_id)
    assert (await _read(api, prev, made.listing_id))["state"] == "checking"

    await run_job(live[0]["id"])
    assert _check(prev, made.listing_id) == ("clean", made.version_id)
    # a verdict is never overwritten by the settle
    await _settle(prev, made.listing_id)
    assert _check(prev, made.listing_id) == ("clean", made.version_id)

    # a pending stamp with no job behind it: not a wait in the read, and the settle closes it
    prev.conn.execute(
        "UPDATE listings SET remote_check_status = 'pending', remote_check_at = NULL "
        "WHERE id = %s",
        [made.listing_id],
    )
    assert (await _read(api, prev, made.listing_id))["state"] != "checking"
    await _settle(prev, made.listing_id)
    assert _check(prev, made.listing_id) == ("failed", made.version_id)


async def test_a_succeeding_content_fix_on_a_submission_ends_checked_clean(
    api: httpx.AsyncClient, prev: PubWorld
) -> None:
    """The settle runs at the submission's status read terminal (the listing is `generating`
    then): the fix version must still get its own dry run and a `clean` verdict on itself."""
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
    assert (await _read(api, prev, made.listing_id))["state"] == "fixing"
    prev.fake.set_fail_fields({})
    await run_job(job["id"])
    fixed_id = prev.listing(made.listing_id)["current_version_id"]
    assert fixed_id != made.version_id
    await _drain_previews(prev, made.listing_id)
    assert _check(prev, made.listing_id) == ("clean", fixed_id)
    body = await _read(api, prev, made.listing_id)
    assert body["state"] == "fix_ready", body
    assert body["remote_check"]["status"] == "clean", body["remote_check"]


async def test_regenerate_after_a_failed_fix_reaches_a_checked_version(
    api: httpx.AsyncClient, prev: PubWorld
) -> None:
    """The read's `regenerate` next step after the B2-1 case: a regeneration leaves `stopped`
    and the new version is checked with the marketplace (the settled `failed` stamp of the old
    version never sticks)."""
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
    body = await _read(api, prev, made.listing_id)
    assert (body["state"], body["next_step"]["kind"]) == ("stopped", "regenerate"), body

    headers = {**bearer_for(prev.world, OPERATOR), "Idempotency-Key": str(uuid.uuid4())}
    response = await api.post(f"/api/v1/listings/{made.listing_id}/regenerate", headers=headers)
    assert response.status_code == 202, response.text
    await run_job(uuid.UUID(response.json()["job_id"]))
    row = prev.listing(made.listing_id)
    assert row["current_version_id"] not in (None, made.version_id), row["status"]
    await _drain_previews(prev, made.listing_id)
    assert _check(prev, made.listing_id) == ("clean", row["current_version_id"])
    after = await _read(api, prev, made.listing_id)
    assert after["state"] != "stopped" and after["next_step"]["kind"] != "regenerate", after
