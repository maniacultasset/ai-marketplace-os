"""SI11 T14 and fix round 1 (AC14; P:D10; CP-D9; invariant 9): the anti-loop of the resolution
engine on the in-process adapter. The cap moves a `failed` listing to
`blocked/corrections_exhausted` and leaves a `blocked` one as it is; a value the marketplace
refuses after Kuinn repaired it goes to staff, never a second automatic guess; the cycle count
reads only this listing's and tenant's `resolution` jobs since the last human event. The
marketplace's enum per attribute is simulated (`_enums`), as in `test_auto_resolution.py`."""

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
from .publish_support import PubWorld, approved, publish_and_run, publish_one, run_job
from .review_support import generated

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["prev"]


@pytest.fixture
def marketplace_enum(monkeypatch: pytest.MonkeyPatch) -> dict[str, tuple[str, ...]]:
    from app.services.resolution import run

    enums: dict[str, tuple[str, ...]] = {"color": ("BLUE", "RED"), "material": ("COTTON",)}

    async def fake(uow: Any, source: Any, module: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        return dict(enums), {}

    monkeypatch.setattr(run, "_enums", fake)
    return enums


def _publish_jobs(pw: PubWorld, listing_id: uuid.UUID) -> list[dict[str, Any]]:
    ids = pw.rows(
        "SELECT id FROM job_runs WHERE listing_id = %s AND kind = 'listing_publish' "
        "ORDER BY queued_at, id",
        [listing_id],
    )
    return [pw.job(row[0]) for row in ids]


def _job(pw: PubWorld, tenant: str, listing_id: uuid.UUID, payload: dict[str, Any]) -> None:
    pw.conn.execute(
        "INSERT INTO job_runs (tenant_id, kind, listing_id, entity_type, entity_id, "
        "payload_json, correlation_id, status, finished_at, started_at) VALUES "
        "(%s, 'listing_status_sync', %s, 'listing', %s, %s, gen_random_uuid(), "
        "'succeeded', now(), now())",
        [
            pw.world.tenant(tenant),
            listing_id,
            listing_id,
            json.dumps({"v": 1, "listing_id": str(listing_id), **payload}),
        ],
    )


def _cycles_used(pw: PubWorld, listing_id: uuid.UUID, count: int) -> None:
    for _ in range(count):
        _job(pw, "acme", listing_id, {"op": "read", "trigger": "resolution"})


async def _cycles(pw: PubWorld, listing_id: uuid.UUID, tenant: str = "acme") -> int:
    from app.repositories.uow import tenant_uow

    async with tenant_uow(pw.world.tenant(tenant)) as uow:
        return await uow.resolution.cycles_since_human(listing_id)


async def _failed_on_colour(api: httpx.AsyncClient, pw: PubWorld) -> Any:
    made = await approved(pw)
    pw.fake.set_fail_fields(
        {"attributes.color": ("99300", "Bad colour.", ErrorCode.VALUE_NOT_ALLOWED)}
    )
    await publish_and_run(api, pw, made)
    return made


# ---- the cap: a repair written at 3 cycles, and no 4th automatic job ----


async def test_cap_moves_a_failed_listing_to_corrections_exhausted(
    api: httpx.AsyncClient, prev: PubWorld, marketplace_enum: Any
) -> None:
    made = await approved(prev)
    prev.fake.set_fail_fields(
        {"attributes.color": ("99300", "Bad colour.", ErrorCode.VALUE_NOT_ALLOWED)}
    )
    item = await publish_one(api, prev, made)
    _cycles_used(prev, made.listing_id, 3)  # after the member's publish: three automatic cycles
    assert await _cycles(prev, made.listing_id) == 3
    await run_job(uuid.UUID(item["job_id"]))
    rules = prev.rows(
        "SELECT attribute FROM listing_repairs WHERE listing_id = %s", [made.listing_id]
    )
    assert [r[0] for r in rules] == ["color"]  # the repair is written (a real repair step)
    assert len(_publish_jobs(prev, made.listing_id)) == 1  # but no 4th automatic job
    listing = prev.listing(made.listing_id)
    assert (listing["status"], listing["status_reason"]) == ("blocked", "corrections_exhausted")
    ((status,),) = prev.rows(
        "SELECT status FROM listing_issues WHERE listing_id = %s", [made.listing_id]
    )
    assert status == "open"  # never closed auto_fixed without the resend that carries it


async def test_cap_leaves_a_blocked_listing_blocked(prev: PubWorld, marketplace_enum: Any) -> None:
    prev.fake.preview_errors = [
        ErrorItem(
            field="attributes.color",
            error_code=ErrorCode.VALUE_NOT_ALLOWED,
            severity=Severity.ERROR,
            message="The value provided for 'color' is not one of the allowed values.",
            source=ErrorSource.AMAZON_ADAPTER,
            retryable=False,
            raw_code="99300",
        )
    ]
    made = await generated(prev.world)
    _cycles_used(prev, made.listing_id, 3)
    (first,) = preview_jobs(prev, made.listing_id)
    await run_job(first["id"])
    assert len(preview_jobs(prev, made.listing_id)) == 1
    listing = prev.listing(made.listing_id)
    assert (listing["status"], listing["status_reason"]) == ("blocked", "validation_errors")


# ---- one automatic guess per attribute (moved from repro/, R2) ----


async def test_a_refused_repair_goes_to_staff_not_auto_repair(
    api: httpx.AsyncClient, prev: PubWorld, marketplace_enum: dict[str, tuple[str, ...]]
) -> None:
    from app.repositories.uow import tenant_uow
    from app.services.resolution.run import resolve

    made = await _failed_on_colour(api, prev)
    resend = _publish_jobs(prev, made.listing_id)[1]
    marketplace_enum["color"] = ("blue",)  # the repaired value is refused with another code
    prev.fake.set_fail_fields(
        {"attributes.color": ("99301", "Bad colour.", ErrorCode.VALUE_NOT_ALLOWED)}
    )
    await run_job(resend["id"])
    assert prev.listing(made.listing_id)["status"] == "failed"
    assert len(_publish_jobs(prev, made.listing_id)) == 2  # no second guess resent
    async with tenant_uow(prev.world.tenant("acme")) as uow:
        listing = await uow.listings.get(made.listing_id)
        job = await uow.job_runs.get_by_id(resend["id"])
        assert job is not None
        result = await resolve(uow, listing, entry="submission", job=job)
    assert result is not None and not result.repairs
    assert [(s.lane, s.action) for s in result.steps] == [("staff", "repair_rejected")]


async def test_a_refused_insert_is_never_left_in_auto_repair(
    api: httpx.AsyncClient,
    prev: PubWorld,
    marketplace_enum: dict[str, tuple[str, ...]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The store refusing a planned repair (either unique index) re-plans the issue as staff."""
    from app.repositories.repairs import RepairRepository

    async def refused(self: Any, new: Any) -> None:
        return None

    monkeypatch.setattr(RepairRepository, "insert", refused)
    made = await _failed_on_colour(api, prev)
    assert len(_publish_jobs(prev, made.listing_id)) == 1
    from app.repositories.uow import tenant_uow
    from app.services.resolution.run import resolve

    async with tenant_uow(prev.world.tenant("acme")) as uow:
        listing = await uow.listings.get(made.listing_id)
        job = await uow.job_runs.get_by_id(_publish_jobs(prev, made.listing_id)[0]["id"])
        assert job is not None
        result = await resolve(uow, listing, entry="submission", job=job)
    assert result is not None
    assert [(s.lane, s.action) for s in result.steps] == [("staff", "repair_rejected")]


# ---- the count: resolution jobs only, since the last human event, this tenant only ----


async def test_only_resolution_jobs_count(prev: PubWorld) -> None:
    made = await approved(prev)
    _job(prev, "acme", made.listing_id, {"op": "read"})  # a scheduled read: no cycle
    _job(prev, "acme", made.listing_id, {"op": "preview", "trigger": "system"})
    assert await _cycles(prev, made.listing_id) == 0
    _cycles_used(prev, made.listing_id, 1)
    assert await _cycles(prev, made.listing_id) == 1


async def test_a_human_event_resets_the_count(api: httpx.AsyncClient, prev: PubWorld) -> None:
    made = await approved(prev)
    _cycles_used(prev, made.listing_id, 2)
    assert await _cycles(prev, made.listing_id) == 2
    await publish_and_run(api, prev, made)  # a member's publish is a human event
    assert await _cycles(prev, made.listing_id) == 0


async def test_another_tenants_jobs_never_count(prev: PubWorld) -> None:
    made = await approved(prev)
    other = await generated(prev.world, "operator@bharat.test")
    for _ in range(3):
        _job(prev, "bharat", other.listing_id, {"op": "read", "trigger": "resolution"})
    assert await _cycles(prev, made.listing_id) == 0
    assert await _cycles(prev, other.listing_id, "bharat") == 3
    assert await _cycles(prev, other.listing_id, "acme") == 0  # RLS and the repository scope
