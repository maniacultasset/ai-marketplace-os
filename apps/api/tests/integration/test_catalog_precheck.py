"""SI13 T5 (AC4, AC5; P:A3, D13-D14; CP-D6, CP-D13): the catalog and brand-restriction pre-check
inside the marketplace dry-run job, on the in-process adapter. Its items join the preview's
(`|preview` rows), an ERROR blocks approval through SI11's gate, a restriction opens the
`permission_decision` task with Amazon's Seller Central link, and a failed check never blocks."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import Severity
from mkos_schemas.errors import ErrorItem

from .conftest import LOCAL_HOSTS
from .preview_support import prev, preview_jobs, preview_rows
from .publish_support import PubWorld
from .review_support import REVIEWER, edited, generated, item, post_approve, run_job

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["prev"]

LINK = "https://sellercentral.amazon.in/hz/approvalrequest?asin=B0ABCDEF12"
ASIN = "B0ABCDEF12"


def _catalog(code: str, severity: Severity, path: str = "facts.identifier") -> ErrorItem:
    from app.adapters.amazon.mapping import error

    text = {
        "catalog:other_brand": "This barcode belongs to another brand on Amazon.",
        "catalog:same_brand": "Amazon already has this product; your offer will join it.",
        "catalog:similar_brand": "Amazon lists this barcode under a similar brand name.",
    }[code]
    found = error(path, ErrorCode.IDENTIFIER_DUPLICATE, text)
    return found.model_copy(update={"severity": severity, "raw_code": code})


def _restricted(link: str) -> ErrorItem:
    from app.adapters.amazon.precheck import from_restrictions

    reply = {
        "restrictions": [
            {
                "reasons": [
                    {
                        "reasonCode": "APPROVAL_REQUIRED",
                        "message": "You need approval.",
                        "links": [{"resource": link}],
                    }
                ]
            }
        ]
    }
    (found,) = from_restrictions(reply)
    return found


def _script(pw: PubWorld, *items: ErrorItem, **over: Any) -> None:
    from app.adapters.base import PrecheckResult

    pw.fake.precheck_result = PrecheckResult(
        items=items, checked=1, scope=("facts.identifier", "listing"), **over
    )


async def _checked(pw: PubWorld, listing_id: uuid.UUID) -> dict[str, Any]:
    for job in preview_jobs(pw, listing_id):
        if job["status"] == "queued":
            await run_job(job["id"])
    return preview_jobs(pw, listing_id)[0]


def _open(pw: PubWorld, listing_id: uuid.UUID) -> list[dict[str, Any]]:
    return [r for r in preview_rows(pw, listing_id) if r["status"] == "open"]


# ---- AC5: another brand's barcode blocks; the same or a similar brand warns ----


async def test_other_brand_blocks_approval(api: httpx.AsyncClient, prev: PubWorld) -> None:
    _script(prev, _catalog("catalog:other_brand", Severity.ERROR))
    made = await generated(prev.world)
    job = await _checked(prev, made.listing_id)
    (row,) = _open(prev, made.listing_id)
    assert (row["field_path"], row["error_code"], row["severity"]) == (
        "facts.identifier",
        "IDENTIFIER_DUPLICATE",
        "error",
    )
    assert row["marketplace_code"] == "catalog:other_brand"
    assert job["result_json"]["precheck"] == {"checked": 1, "matched": [], "failed": 0}
    assert prev.fake.precheck_calls == [True]  # restrictions on: one switch (cut #3 sets False)
    listing = prev.listing(made.listing_id)
    assert listing["remote_check_status"] == "issues"
    assert (listing["status"], listing["status_reason"]) == ("blocked", "validation_errors")
    response = await post_approve(api, prev.world, REVIEWER, [item(made)])
    assert response.json()["items"][0]["http_status"] in (409, 422)


@pytest.mark.parametrize("code", ["catalog:same_brand", "catalog:similar_brand"])
async def test_same_or_similar_brand_only_warns(
    api: httpx.AsyncClient, prev: PubWorld, code: str
) -> None:
    matched = (("SKU", ASIN),) if code == "catalog:same_brand" else ()
    _script(prev, _catalog(code, Severity.WARNING), matched=matched)
    made = await generated(prev.world)
    job = await _checked(prev, made.listing_id)
    (row,) = _open(prev, made.listing_id)
    assert (row["severity"], row["marketplace_code"]) == ("warning", code)
    assert job["result_json"]["precheck"]["matched"] == ([ASIN] if matched else [])
    assert prev.listing(made.listing_id)["remote_check_status"] == "clean"
    response = await post_approve(api, prev.world, REVIEWER, [item(made)])
    assert response.json()["items"][0]["http_status"] == 200


# ---- AC4: a restricted brand opens the Seller Central step with the link ----


async def _permission_task(pw: PubWorld, listing_id: uuid.UUID) -> tuple[Any, ...]:
    (task,) = pw.rows(
        "SELECT t.kind, t.question FROM listing_issues i JOIN human_tasks t "
        "ON t.id = i.human_task_id WHERE i.listing_id = %s AND i.status = 'open' "
        "AND i.error_code = 'ACCOUNT_PERMISSION_DENIED'",
        [listing_id],
    )
    return task


async def test_restricted_brand_task_carries_the_link(prev: PubWorld) -> None:
    _script(prev, _restricted(LINK))
    made = await generated(prev.world)
    await _checked(prev, made.listing_id)
    kind, question = await _permission_task(prev, made.listing_id)
    assert kind == "permission_decision" and LINK in question


async def test_restricted_brand_bad_link_is_dropped(prev: PubWorld) -> None:
    _script(prev, _restricted(LINK.replace("https://", "http://")))
    made = await generated(prev.world)
    await _checked(prev, made.listing_id)
    kind, question = await _permission_task(prev, made.listing_id)
    assert kind == "permission_decision" and "http" not in question


# ---- a failed pre-check never blocks ----


async def test_failed_precheck_never_blocks(api: httpx.AsyncClient, prev: PubWorld) -> None:
    from app.adapters.base import PrecheckResult

    prev.fake.precheck_result = PrecheckResult(failed=("SKU",), checked=1)
    made = await generated(prev.world)
    job = await _checked(prev, made.listing_id)
    assert job["status"] == "succeeded"
    assert job["result_json"]["precheck"]["failed"] == 1
    assert _open(prev, made.listing_id) == []
    assert prev.listing(made.listing_id)["remote_check_status"] == "clean"
    response = await post_approve(api, prev.world, REVIEWER, [item(made)])
    assert response.json()["items"][0]["http_status"] == 200


async def test_precheck_adapter_error_never_fails_the_job(prev: PubWorld) -> None:
    from app.adapters.amazon.client import failure
    from app.adapters.base import MarketplaceUnavailable

    prev.fake.precheck_error = MarketplaceUnavailable(
        failure(ErrorCode.MARKETPLACE_UNAVAILABLE, "down")
    )
    made = await generated(prev.world)
    job = await _checked(prev, made.listing_id)
    assert job["status"] == "succeeded"
    assert job["result_json"]["remote_check"] == "clean"
    assert job["result_json"]["precheck"]["failed"] == 1


# ---- switches, replay and closure ----


async def test_precheck_flag_off_makes_no_call(
    prev: PubWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("AMAZON_CATALOG_PRECHECK_ENABLED", "false")
    get_settings.cache_clear()
    made = await generated(prev.world)
    job = await _checked(prev, made.listing_id)
    assert prev.fake.precheck_calls == [] and "precheck" not in job["result_json"]


async def test_preview_flag_off_runs_no_job(
    prev: PubWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("AMAZON_VALIDATION_PREVIEW_ENABLED", "false")
    get_settings.cache_clear()
    made = await generated(prev.world)
    assert preview_jobs(prev, made.listing_id) == []
    assert prev.fake.precheck_calls == []


async def test_replay_searches_once(prev: PubWorld) -> None:
    from app.repositories.uow import tenant_uow
    from app.services.remote_preview import request_preview

    made = await generated(prev.world)
    await _checked(prev, made.listing_id)
    async with tenant_uow(prev.world.tenant("acme")) as uow:
        listing = await uow.listings.get(made.listing_id)
        assert await request_preview(uow, listing, made.version_id) is None
    assert len(preview_jobs(prev, made.listing_id)) == 1
    assert prev.fake.precheck_calls == [True]


async def test_a_fixed_barcode_closes_the_error(api: httpx.AsyncClient, prev: PubWorld) -> None:
    from app.adapters.base import PrecheckResult

    _script(prev, _catalog("catalog:other_brand", Severity.ERROR))
    made = await generated(prev.world)
    await _checked(prev, made.listing_id)
    assert len(_open(prev, made.listing_id)) == 1
    prev.fake.precheck_result = PrecheckResult(checked=1, scope=("facts.identifier",))
    await edited(api, prev.world, made)
    for job in preview_jobs(prev, made.listing_id):
        if job["status"] == "queued":
            await run_job(job["id"])
    assert _open(prev, made.listing_id) == []


async def test_another_tenant_sees_no_row(prev: PubWorld) -> None:
    _script(prev, _catalog("catalog:other_brand", Severity.ERROR))
    made = await generated(prev.world)
    await _checked(prev, made.listing_id)

    def seen(slug: str) -> tuple[Any, ...]:
        conn = prev.conn
        with conn.transaction():
            conn.execute(
                "SELECT set_config('app.tenant_id', %s, true)", [str(prev.world.tenant(slug))]
            )
            conn.execute("SET LOCAL ROLE app_rw")
            issues = conn.execute(
                "SELECT count(*) FROM listing_issues WHERE listing_id = %s", [made.listing_id]
            ).fetchone()
            jobs = conn.execute(
                "SELECT count(*) FROM job_runs WHERE listing_id = %s AND result_json ? 'precheck'",
                [made.listing_id],
            ).fetchone()
        assert issues is not None and jobs is not None
        return issues[0], jobs[0]

    assert seen("acme")[0] >= 1 and seen("acme")[1] == 1  # the owner sees its row and job
    assert seen("bharat") == (0, 0)
