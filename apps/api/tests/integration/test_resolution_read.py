"""SI11 T20 (AC18, AC19): `GET /api/v1/listings/{id}/resolution` and the inbox's `lane`/`cause`,
with the route's case matrix."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource, Severity
from mkos_schemas.errors import ErrorItem

from .conftest import LOCAL_HOSTS
from .preview_support import prev, preview_jobs
from .products_support import bad_bearer
from .publish_support import PubWorld, run_job
from .review_support import OPERATOR, REVIEWER, generated

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["prev"]


def _url(listing_id: Any) -> str:
    return f"/api/v1/listings/{listing_id}/resolution"


def _error(field: str, code: ErrorCode, raw: str, message: str) -> ErrorItem:
    return ErrorItem(
        field=field,
        error_code=code,
        severity=Severity.ERROR,
        message=message,
        source=ErrorSource.AMAZON_ADAPTER,
        retryable=False,
        raw_code=raw,
    )


COLOR = _error(
    "attributes.color",
    ErrorCode.VALUE_NOT_ALLOWED,
    "99300",
    "The value provided for 'color' is not one of the allowed values.",
)
UNKNOWN = _error(
    "listing", ErrorCode.MARKETPLACE_UNKNOWN_ERROR, "77777", "Something unusual went wrong."
)


async def _previewed(pw: PubWorld, errors: list[ErrorItem]) -> Any:
    pw.fake.preview_errors = errors
    made = await generated(pw.world)
    for job in preview_jobs(pw, made.listing_id):
        if job["status"] == "queued":
            await run_job(job["id"])
    return made


async def _read(
    api: httpx.AsyncClient, pw: PubWorld, listing_id: Any, email: str = OPERATOR
) -> Any:
    response = await api.get(_url(listing_id), headers=pw.world.headers(email))
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.endpoint("listings_resolution", case="success")
async def test_a_seller_question_reads_needs_you(api: httpx.AsyncClient, prev: PubWorld) -> None:
    made = await _previewed(prev, [COLOR])
    body = await _read(api, prev, made.listing_id)
    assert body["state"] == "needs_you"
    assert body["max_cycles"] == 3 and body["cycles_used"] == 0
    assert body["remote_check"]["status"] == "issues"
    (attempt,) = body["attempts"]
    assert (attempt["kind"], attempt["number"], attempt["actor"], attempt["outcome"]) == (
        "precheck",
        0,
        "system",
        "issues",
    )
    assert [
        (f["field"], f["lane"], f["cause"], f["marketplace_code"]) for f in attempt["failed"]
    ] == [("attributes.color", "seller", "seller_fact", "99300")]
    task = prev.scalar(
        "SELECT human_task_id FROM listing_issues WHERE listing_id = %s AND status = 'open'",
        [made.listing_id],
    )
    assert body["next_step"]["kind"] == "answer"
    assert body["next_step"]["task_ids"] == [str(task)]


async def test_a_clean_listing_reads_idle(api: httpx.AsyncClient, prev: PubWorld) -> None:
    made = await _previewed(prev, [])
    body = await _read(api, prev, made.listing_id, REVIEWER)
    assert body["state"] == "idle"
    assert [a["outcome"] for a in body["attempts"]] == ["clean"]
    assert body["next_step"] == {"kind": "none", "task_ids": [], "text": ""}


async def test_a_queued_check_reads_checking(api: httpx.AsyncClient, prev: PubWorld) -> None:
    prev.fake.preview_errors = []
    made = await generated(prev.world)  # the dry run is queued, not run
    body = await _read(api, prev, made.listing_id)
    assert body["state"] == "checking"
    assert [a["outcome"] for a in body["attempts"]] == ["pending"]
    assert body["next_step"]["kind"] == "wait"


async def test_a_staff_row_names_staff_in_the_read_and_the_inbox(
    api: httpx.AsyncClient, prev: PubWorld
) -> None:
    made = await _previewed(prev, [UNKNOWN])
    body = await _read(api, prev, made.listing_id)
    (failure,) = body["attempts"][0]["failed"]
    assert (failure["lane"], failure["cause"]) == ("staff", "mapping")
    page = await api.get(
        "/api/v1/issues",
        headers=prev.world.headers(OPERATOR),
        params={"listing_id": str(made.listing_id)},
    )
    assert page.status_code == 200, page.text
    rows = [r for r in page.json()["data"] if r["listing_id"] == str(made.listing_id)]
    errors = [r for r in rows if r["severity"] == "error"]
    assert errors and all((r["lane"], r["cause"]) == ("staff", "mapping") for r in errors)
    assert all(r["lane"] is None for r in rows if r["severity"] == "warning")


async def test_cycles_used_is_bounded(api: httpx.AsyncClient, prev: PubWorld) -> None:
    made = await _previewed(prev, [UNKNOWN])
    (job,) = preview_jobs(prev, made.listing_id)
    for n in range(5):
        prev.conn.execute(
            "INSERT INTO job_runs (id, tenant_id, kind, entity_type, entity_id, listing_id, status, "
            "payload_json, correlation_id, queued_at, started_at, finished_at) "
            "SELECT %s, tenant_id, kind, entity_type, entity_id, listing_id, 'succeeded', "
            "payload_json || jsonb_build_object('trigger', 'resolution'), correlation_id, "
            "q, q, q FROM job_runs, LATERAL (SELECT now() + make_interval(secs => %s) AS q) t "
            "WHERE id = %s",
            [uuid.uuid4(), n + 1, job["id"]],
        )
    body = await _read(api, prev, made.listing_id)
    assert body["cycles_used"] == 3
    assert body["state"] == "stopped"
    assert body["next_step"]["kind"] == "resume"


@pytest.mark.endpoint("listings_resolution", case="validation")
async def test_resolution_bad_uuid_422(api: httpx.AsyncClient, prev: PubWorld) -> None:
    response = await api.get(_url("nope"), headers=prev.world.headers(OPERATOR))
    assert response.status_code == 422


@pytest.mark.endpoint("listings_resolution", case="missing_auth")
async def test_resolution_missing_auth_401(api: httpx.AsyncClient, prev: PubWorld) -> None:
    assert (await api.get(_url(uuid.uuid4()))).status_code == 401


async def _invalid(api: httpx.AsyncClient, pw: PubWorld, variant: str) -> None:
    response = await api.get(_url(uuid.uuid4()), headers=bad_bearer(pw.world, variant, OPERATOR))
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "UNAUTHENTICATED"


@pytest.mark.endpoint("listings_resolution", case="invalid_token", variant="expired")
async def test_resolution_invalid_token_expired(api: httpx.AsyncClient, prev: PubWorld) -> None:
    await _invalid(api, prev, "expired")


@pytest.mark.endpoint("listings_resolution", case="invalid_token", variant="bad_signature")
async def test_resolution_invalid_token_bad_signature(
    api: httpx.AsyncClient, prev: PubWorld
) -> None:
    await _invalid(api, prev, "bad_signature")


@pytest.mark.endpoint("listings_resolution", case="invalid_token", variant="wrong_typ")
async def test_resolution_invalid_token_wrong_typ(api: httpx.AsyncClient, prev: PubWorld) -> None:
    await _invalid(api, prev, "wrong_typ")


async def test_resolution_every_role_reads(api: httpx.AsyncClient, prev: PubWorld) -> None:
    """Every role holds `tenant:read`, so the route has no 403 case."""
    made = await _previewed(prev, [])
    for email in ("owner@acme.test", OPERATOR, REVIEWER, "admin@acme.test"):
        assert (
            await api.get(_url(made.listing_id), headers=prev.world.headers(email))
        ).status_code == 200


@pytest.mark.endpoint("listings_resolution", case="wrong_tenant")
async def test_resolution_other_tenant_404(api: httpx.AsyncClient, prev: PubWorld) -> None:
    made = await _previewed(prev, [COLOR])
    response = await api.get(
        _url(made.listing_id), headers=prev.world.headers("operator@bharat.test")
    )
    assert response.status_code == 404
    assert "attributes.color" not in response.text


@pytest.mark.endpoint("listings_resolution", case="not_found")
async def test_resolution_not_found_404(api: httpx.AsyncClient, prev: PubWorld) -> None:
    response = await api.get(_url(uuid.uuid4()), headers=prev.world.headers(OPERATOR))
    assert response.status_code == 404
