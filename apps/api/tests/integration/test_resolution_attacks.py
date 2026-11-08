"""SI11 tester attacks (AC4, AC7, AC14, AC19): the resolution engine on the in-process adapter
under replay, pause, tenant overlap and the cycle cap on the pre-approval path. The marketplace's
enum per attribute is simulated (`_enums`), as in `test_auto_resolution.py`."""

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
from .review_support import generated, new_product

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["prev"]

ENUMS: dict[str, tuple[str, ...]] = {
    "color": ("BLUE", "RED"),
    "material": ("COTTON", "SILK"),
    "country_of_origin": ("in", "cn"),
    "manufacturer": ("test mills pvt ltd",),
}


@pytest.fixture
def marketplace_enum(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.resolution import run

    async def enums(uow: Any, source: Any, module: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        return dict(ENUMS), {}

    monkeypatch.setattr(run, "_enums", enums)


def _error(attribute: str, raw: str = "99300") -> ErrorItem:
    return ErrorItem(
        field=f"attributes.{attribute}",
        error_code=ErrorCode.VALUE_NOT_ALLOWED,
        severity=Severity.ERROR,
        message=f"The value provided for '{attribute}' is not one of the allowed values.",
        source=ErrorSource.AMAZON_ADAPTER,
        retryable=False,
        raw_code=raw,
    )


def _repairs(pw: PubWorld, listing_id: uuid.UUID) -> list[tuple[Any, ...]]:
    return pw.rows(
        "SELECT tenant_id, attribute, rule FROM listing_repairs WHERE listing_id = %s "
        "ORDER BY created_at",
        [listing_id],
    )


def _publish_jobs(pw: PubWorld, listing_id: uuid.UUID) -> list[dict[str, Any]]:
    ids = pw.rows(
        "SELECT id FROM job_runs WHERE listing_id = %s AND kind = 'listing_publish' "
        "ORDER BY queued_at, id",
        [listing_id],
    )
    return [pw.job(row[0]) for row in ids]


async def _failed_on_colour(api: httpx.AsyncClient, pw: PubWorld) -> Any:
    made = await approved(pw)
    pw.fake.set_fail_fields(
        {"attributes.color": ("99300", "Bad colour.", ErrorCode.VALUE_NOT_ALLOWED)}
    )
    await publish_and_run(api, pw, made)
    return made


def _resolution_jobs(pw: PubWorld, listing_id: uuid.UUID, count: int) -> None:
    for _ in range(count):
        pw.conn.execute(
            "INSERT INTO job_runs (tenant_id, kind, listing_id, entity_type, entity_id, "
            "payload_json, correlation_id, status, finished_at, started_at) VALUES "
            "(%s, 'listing_status_sync', %s, 'listing', %s, %s, gen_random_uuid(), "
            "'succeeded', now(), now())",
            [
                pw.world.tenant("acme"),
                listing_id,
                listing_id,
                json.dumps(
                    {"v": 1, "listing_id": str(listing_id), "op": "read", "trigger": "resolution"}
                ),
            ],
        )


# ---- AC14 on the pre-approval path: three automatic re-checks, then nothing ----


async def test_preview_path_never_enqueues_a_fourth_recheck(
    prev: PubWorld, marketplace_enum: None
) -> None:
    made = await generated(prev.world)
    # three automatic cycles already used (this world has one non-fact enum attribute, so the
    # earlier cycles are recorded rows, as in test_auto_resolution's cap test)
    _resolution_jobs(prev, made.listing_id, 3)
    rounds = ["color"]
    for number, attribute in enumerate(rounds, start=1):
        prev.fake.preview_errors = [_error(attribute)]
        jobs = preview_jobs(prev, made.listing_id)
        assert len(jobs) == number, (attribute, len(jobs))
        await run_job(jobs[-1]["id"])
    # the repair is written, but the 4th automatic job (a re-check) is never enqueued
    assert [r[1] for r in _repairs(prev, made.listing_id)] == rounds
    jobs = preview_jobs(prev, made.listing_id)
    assert len(jobs) == 1
    listing = prev.listing(made.listing_id)
    assert listing["status"] == "blocked"  # CP-D9: from blocked it stays
    assert listing["current_version_id"] == made.version_id  # no new version
    (last,) = prev.rows(
        "SELECT status FROM listing_issues WHERE listing_id = %s AND field_path = %s",
        [made.listing_id, "attributes.color"],
    )
    assert last[0] == "open"  # never closed auto_fixed without the re-check that carries it


# ---- duplicate delivery of the submission hook ----


async def test_submission_hook_replay_enqueues_one_resend(
    api: httpx.AsyncClient, prev: PubWorld, marketplace_enum: None
) -> None:
    from app.repositories.uow import tenant_uow
    from app.services.resolution.run import resolve

    made = await _failed_on_colour(api, prev)
    first = _publish_jobs(prev, made.listing_id)[0]
    for _ in range(2):
        async with tenant_uow(prev.world.tenant("acme")) as uow:
            listing = await uow.listings.get(made.listing_id)
            job = await uow.job_runs.get_by_id(first["id"])
            assert job is not None
            result = await resolve(uow, listing, entry="submission", job=job)
            assert result is not None and result.job_id is None
    assert len(_publish_jobs(prev, made.listing_id)) == 2
    assert len(_repairs(prev, made.listing_id)) == 1
    assert prev.listing(made.listing_id)["status"] == "publishing"


# ---- a paused listing is never resolved ----


async def test_paused_listing_gets_no_repair_and_no_recheck(
    prev: PubWorld, marketplace_enum: None
) -> None:
    prev.fake.preview_errors = [_error("color")]
    made = await generated(prev.world)
    (first,) = preview_jobs(prev, made.listing_id)
    prev.conn.execute("UPDATE listings SET paused_at = now() WHERE id = %s", [made.listing_id])
    await run_job(first["id"])
    assert _repairs(prev, made.listing_id) == []
    assert len(preview_jobs(prev, made.listing_id)) == 1


# ---- AC19: same SKU and code in two tenants never share a repair or an issue ----


async def test_two_tenants_same_sku_never_share_a_repair(
    prev: PubWorld, marketplace_enum: None
) -> None:
    sku = f"SI11-{uuid.uuid4().hex[:8].upper()}"
    prev.fake.preview_errors = [_error("color")]
    made = {}
    for slug in ("acme", "bharat"):
        email = f"operator@{slug}.test"
        await new_product(prev.world, email, sku=sku)
        made[slug] = await generated(prev.world, email, sku=sku)
        (first,) = preview_jobs(prev, made[slug].listing_id)
        await run_job(first["id"])
    for slug in ("acme", "bharat"):
        rows = _repairs(prev, made[slug].listing_id)
        assert [(r[0], r[1]) for r in rows] == [(prev.world.tenant(slug), "color")]
    keys = prev.rows(
        "SELECT tenant_id, listing_id FROM listing_issues WHERE dedupe_key LIKE %s",
        ["amazon_adapter|attributes.color|%|preview"],
    )
    pairs = {(t, lid) for t, lid in keys if lid in {m.listing_id for m in made.values()}}
    assert pairs == {(prev.world.tenant(s), made[s].listing_id) for s in made}
    # RLS: acme's session never reads bharat's repair
    from app.repositories.uow import tenant_uow

    async with tenant_uow(prev.world.tenant("acme")) as uow:
        assert await uow.listing_repairs.live_for_listing(made["bharat"].listing_id) == []
