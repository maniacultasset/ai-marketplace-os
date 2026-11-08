"""SI11 fix round 2 (R2-3): a fact changed and then changed back replays the first
preview's key. The replay takes the first verdict, but the ERROR the in-between preview opened
is never closed by any new preview."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource, Severity
from mkos_schemas.errors import ErrorItem

from .conftest import LOCAL_HOSTS
from .preview_support import prev, preview_jobs, preview_rows
from .publish_support import PubWorld, pub
from .review_support import generated, run_job

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["prev", "pub"]


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


async def _run_previews(pw: PubWorld, listing_id: uuid.UUID) -> None:
    for job in preview_jobs(pw, listing_id):
        if job["status"] == "queued":
            await run_job(job["id"])


async def _set_colour(
    api: httpx.AsyncClient, pw: PubWorld, listing_id: uuid.UUID, value: Any
) -> None:
    from .review_support import OPERATOR

    product_id = pw.listing(listing_id)["product_id"]
    url = f"/api/v1/products/{product_id}"
    got = await api.get(url, headers=pw.world.headers(OPERATOR))
    assert got.status_code == 200, got.text
    patched = await api.patch(
        url,
        headers={**pw.world.headers(OPERATOR), "If-Match": got.headers["ETag"]},
        json={"attributes": {"color": value}},
    )
    assert patched.status_code == 200, patched.text
    response = await api.post(
        "/api/v1/listings/validate",
        headers=pw.world.headers(OPERATOR),
        json={"listing_id": str(listing_id)},
    )
    assert response.status_code == 200, response.text
    await _run_previews(pw, listing_id)


async def test_a_fact_reverted_to_a_clean_value_unblocks_approval(
    api: httpx.AsyncClient, prev: PubWorld
) -> None:
    from .review_support import REVIEWER, item, only_item, post_approve

    made = await generated(prev.world)
    await _run_previews(prev, made.listing_id)  # the generation's own check (factory facts)
    # facts A (set by the seller through the API, so A can be restored byte for byte): clean
    await _set_colour(api, prev, made.listing_id, "red")
    await _run_previews(prev, made.listing_id)
    assert prev.listing(made.listing_id)["remote_check_status"] == "clean"
    # facts B: the marketplace refuses the colour
    prev.fake.preview_errors = [_colour_error()]
    await _set_colour(api, prev, made.listing_id, "black")
    await _run_previews(prev, made.listing_id)
    row = prev.listing(made.listing_id)
    assert row["remote_check_status"] == "issues", (
        row["status"],
        [(j["status"], j["payload_json"]) for j in preview_jobs(prev, made.listing_id)],
    )
    # back to facts A, which the marketplace accepted
    prev.fake.preview_errors = []
    await _set_colour(api, prev, made.listing_id, "red")
    await _run_previews(prev, made.listing_id)
    jobs = preview_jobs(prev, made.listing_id)
    open_rows = [r for r in preview_rows(prev, made.listing_id) if r["status"] == "open"]
    check = prev.listing(made.listing_id)["remote_check_status"]
    approve = await post_approve(api, prev.world, REVIEWER, [item(made)])
    status = only_item(approve)["http_status"]
    # truthful: either a fresh check ran and closed B's error, or the replayed "clean" matches
    # an inbox with no open pre-check error. Never "clean" with approval refused.
    assert (check, status, len(open_rows)) == ("clean", 200, 0), (
        check,
        status,
        len(open_rows),
        [(j["status"], j["result_json"]) for j in jobs],
        approve.text,
    )


async def test_a_replayed_clean_key_never_hides_an_open_precheck_error(
    api: httpx.AsyncClient, prev: PubWorld
) -> None:
    """The replay half alone: a re-check of facts A after B's ERROR opened (the resolution
    trigger may run from blocked/validation_errors) replays A's key and writes A's verdict,
    while B's ERROR stays open on the same version."""
    from app.repositories.uow import tenant_uow
    from app.services.remote_preview import request_preview

    made = await generated(prev.world)
    await _run_previews(prev, made.listing_id)  # the generation's own check (factory facts)
    await _set_colour(api, prev, made.listing_id, "red")
    prev.fake.preview_errors = [_colour_error()]
    await _set_colour(api, prev, made.listing_id, "black")
    await _set_colour(api, prev, made.listing_id, "red")  # back to A: validate is a no-op
    prev.fake.preview_errors = []
    async with tenant_uow(prev.world.tenant("acme")) as uow:
        listing = await uow.listings.get(made.listing_id)
        replay = await request_preview(uow, listing, made.version_id, trigger="resolution")
    await _run_previews(prev, made.listing_id)
    open_rows = [r for r in preview_rows(prev, made.listing_id) if r["status"] == "open"]
    check = prev.listing(made.listing_id)["remote_check_status"]
    # either a fresh check ran (replay is a job id) or the verdict matches the open rows
    assert replay is not None or not (check == "clean" and open_rows), (replay, check, open_rows)
