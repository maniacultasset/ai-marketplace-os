"""SI11 T12-T14 (AC4, AC7, AC8, AC14; P:B4, D2, D10; T41): the resolution engine end to end on the
in-process adapter. A stored Product Type Definition enum is simulated (`_enums`), so a value our
template sends can still be refused by the marketplace and folded by the repairer."""

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
from .review_support import generated

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["prev"]


@pytest.fixture
def marketplace_enum(monkeypatch: pytest.MonkeyPatch) -> None:
    """The marketplace spells the colour differently from the template (a stored PTD enum)."""
    from app.services.resolution import run

    async def enums(uow: Any, source: Any, module: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        return {"color": ("RED", "BLUE", "NAVY", "MAROON")}, {}

    monkeypatch.setattr(run, "_enums", enums)


def _colour_error() -> ErrorItem:
    return ErrorItem(
        field="attributes.color",
        error_code=ErrorCode.VALUE_NOT_ALLOWED,
        severity=Severity.ERROR,
        message="The value provided for 'color' is not one of the allowed values.",
        source=ErrorSource.AMAZON_ADAPTER,
        retryable=False,
        raw_code="99300",
    )


def _repairs(pw: PubWorld, listing_id: uuid.UUID) -> list[tuple[Any, ...]]:
    return pw.rows(
        "SELECT rule, attribute, field_path, retired_at FROM listing_repairs WHERE listing_id = %s",
        [listing_id],
    )


def _issue_status(pw: PubWorld, listing_id: uuid.UUID) -> list[tuple[Any, ...]]:
    return pw.rows(
        "SELECT field_path, status, resolution, action_taken FROM listing_issues "
        "WHERE listing_id = %s AND source = 'amazon_adapter' ORDER BY created_at",
        [listing_id],
    )


def _publish_jobs(pw: PubWorld, listing_id: uuid.UUID) -> list[dict[str, Any]]:
    ids = pw.rows(
        "SELECT id FROM job_runs WHERE listing_id = %s AND kind = 'listing_publish' "
        "ORDER BY queued_at, id",
        [listing_id],
    )
    return [pw.job(row[0]) for row in ids]


# ---- AC4: before approval, a repair and one re-check of the same version ----


async def test_preview_issue_is_repaired_and_rechecked(
    prev: PubWorld, marketplace_enum: None
) -> None:
    prev.fake.preview_errors = [_colour_error()]
    made = await generated(prev.world)
    (first,) = preview_jobs(prev, made.listing_id)
    await run_job(first["id"])
    assert [(r[0], r[1]) for r in _repairs(prev, made.listing_id)] == [("enum_fold", "color")]
    jobs = preview_jobs(prev, made.listing_id)
    assert len(jobs) == 2 and jobs[1]["payload_json"]["trigger"] == "resolution"
    assert jobs[1]["payload_json"]["version_id"] == str(made.version_id)  # no new version
    ((_path, status, resolution, action),) = _issue_status(prev, made.listing_id)
    assert (status, resolution, action) == ("auto_fixed", "deterministic_fix", "repair:enum_fold")
    assert prev.listing(made.listing_id)["status"] == "blocked"
    prev.fake.preview_errors = []
    await run_job(jobs[1]["id"])
    listing = prev.listing(made.listing_id)
    assert (listing["status"], listing["remote_check_status"]) == ("validated", "clean")
    assert listing["current_version_id"] == made.version_id


async def test_hook_replay_writes_no_second_repair(prev: PubWorld, marketplace_enum: None) -> None:
    from app.repositories.uow import tenant_uow
    from app.services.resolution.run import resolve

    prev.fake.preview_errors = [_colour_error()]
    made = await generated(prev.world)
    (first,) = preview_jobs(prev, made.listing_id)
    await run_job(first["id"])
    async with tenant_uow(prev.world.tenant("acme")) as uow:
        listing = await uow.listings.get(made.listing_id)
        job = await uow.job_runs.get_by_id(first["id"])
        assert job is not None
        await resolve(uow, listing, entry="preview", job=job)
    assert len(_repairs(prev, made.listing_id)) == 1
    assert len(preview_jobs(prev, made.listing_id)) == 2


async def test_refused_recheck_leaves_the_issue_open(
    prev: PubWorld, marketplace_enum: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.resolution import run

    async def refused(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(run, "request_preview", refused)
    prev.fake.preview_errors = [_colour_error()]
    made = await generated(prev.world)
    (first,) = preview_jobs(prev, made.listing_id)
    await run_job(first["id"])
    assert len(_repairs(prev, made.listing_id)) == 1
    ((_path, status, _resolution, _action),) = _issue_status(prev, made.listing_id)
    assert status == "open"  # CP-D3: closed only when the resend that carries it is enqueued


async def test_executor_failure_keeps_the_preview_result(
    prev: PubWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.resolution import run

    async def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("bug")

    monkeypatch.setattr(run, "_candidates", boom)
    prev.fake.preview_errors = [_colour_error()]
    made = await generated(prev.world)
    (first,) = preview_jobs(prev, made.listing_id)
    await run_job(first["id"])
    assert prev.job(first["id"])["status"] == "succeeded"
    assert prev.listing(made.listing_id)["remote_check_status"] == "issues"
    assert _repairs(prev, made.listing_id) == []


# ---- AC7/AC8: after a rejected submission, one automatic resend (T41) ----


async def _failed_on_colour(api: httpx.AsyncClient, pw: PubWorld) -> Any:
    made = await approved(pw)
    pw.fake.set_fail_fields(
        {"attributes.color": ("99300", "Bad colour.", ErrorCode.VALUE_NOT_ALLOWED)}
    )
    await publish_and_run(api, pw, made)
    return made


async def test_rejected_submission_is_repaired_and_resent(
    api: httpx.AsyncClient, prev: PubWorld, marketplace_enum: None
) -> None:
    made = await _failed_on_colour(api, prev)
    jobs = _publish_jobs(prev, made.listing_id)
    assert len(jobs) == 2, [j["status"] for j in jobs]
    resend = jobs[1]
    assert resend["payload_json"]["trigger"] == "resolution"
    assert resend["created_by"] == jobs[0]["created_by"]  # the requester is copied
    assert prev.listing(made.listing_id)["status"] == "publishing"
    assert prev.listing(made.listing_id)["last_event"] == "auto_resubmit"
    prev.fake.set_fail_fields({})
    await run_job(resend["id"])
    first, second = prev.submissions(made.listing_id)
    assert (second["actor_type"], second["retry_justification"]) == ("system", "changed_input")
    assert second["requested_by_user_id"] == first["requested_by_user_id"]
    (correction,) = second["corrections_json"]
    assert correction["field"] == "attributes.color" and correction["source"] == "adapter_remap"
    assert correction["from_digest"] != correction["to_digest"]
    assert prev.listing(made.listing_id)["status"] == "published"
    audit = prev.rows(
        "SELECT after FROM audit_log WHERE entity_type = 'listing_repair' AND action = %s",
        ["listing_repair.created"],
    )
    assert audit and set(audit[-1][0]) <= {"rule", "attribute"}


async def test_no_resend_when_the_approval_is_not_current() -> None:
    """AC8: the resend needs the approved version to be the current one (the edge's
    `requires_current_approval`, checked before anything is written)."""
    from types import SimpleNamespace

    from app.services.resolution.run import _auto_resubmit

    listing = SimpleNamespace(
        status="failed", approved_version_id=uuid.uuid4(), current_version_id=uuid.uuid4()
    )
    assert await _auto_resubmit(object(), listing, object()) is None  # type: ignore[arg-type]
    listing.status, listing.approved_version_id = "published", listing.current_version_id
    assert await _auto_resubmit(object(), listing, object()) is None  # type: ignore[arg-type]


# ---- AC14: three automatic cycles, then the cap ----


async def test_the_fourth_automatic_job_is_never_enqueued(
    api: httpx.AsyncClient, prev: PubWorld, marketplace_enum: None
) -> None:
    from app.repositories.uow import tenant_uow

    made = await _failed_on_colour(api, prev)
    resend = _publish_jobs(prev, made.listing_id)[1]
    # three resolution jobs since the last human event: the cap holds the next one
    for _ in range(2):
        prev.conn.execute(
            "INSERT INTO job_runs (tenant_id, kind, listing_id, entity_type, entity_id, "
            "payload_json, correlation_id, status, finished_at, started_at) VALUES "
            "(%s, 'listing_status_sync', %s, 'listing', %s, %s, gen_random_uuid(), "
            "'succeeded', now(), now())",
            [
                prev.world.tenant("acme"),
                made.listing_id,
                made.listing_id,
                json.dumps(
                    {
                        "v": 1,
                        "listing_id": str(made.listing_id),
                        "op": "preview",
                        "trigger": "resolution",
                    }
                ),
            ],
        )
    async with tenant_uow(prev.world.tenant("acme")) as uow:
        assert await uow.resolution.cycles_since_human(made.listing_id) == 3
    prev.fake.set_fail_fields(
        {"attributes.size": ("99300", "Bad size.", ErrorCode.VALUE_NOT_ALLOWED)}
    )
    await run_job(resend["id"])
    assert len(_publish_jobs(prev, made.listing_id)) == 2


async def test_a_user_publish_resets_the_cycles(prev: PubWorld) -> None:
    from app.repositories.uow import tenant_uow

    made = await approved(prev)
    async with tenant_uow(prev.world.tenant("acme")) as uow:
        assert await uow.resolution.cycles_since_human(made.listing_id) == 0
    jobs = preview_jobs(prev, made.listing_id)
    assert all("trigger" not in j["payload_json"] for j in jobs)  # a system check is no cycle
