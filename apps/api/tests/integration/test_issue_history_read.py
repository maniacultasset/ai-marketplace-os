"""Feedback item 20: `GET /api/v1/listings/{id}/issue-history`, each marketplace issue with who
handles it and each fix with its before and after values, with the route's case matrix."""

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
    return f"/api/v1/listings/{listing_id}/issue-history"


def _error(field: str, code: ErrorCode, raw: str, message: str, **extra: Any) -> ErrorItem:
    return ErrorItem(
        field=field,
        error_code=code,
        severity=Severity.ERROR,
        message=message,
        source=ErrorSource.AMAZON_ADAPTER,
        retryable=False,
        raw_code=raw,
        **extra,
    )


COLOR = _error(
    "attributes.color",
    ErrorCode.VALUE_NOT_ALLOWED,
    "90244",
    "Amazon accepts only values from its own list for this detail, and the value sent is not "
    "on it. Marketplace message: The value provided for 'color' is not one of the allowed values.",
    value_submitted="Navy blue",
    suggestion="Pick a value from Amazon's list for that detail, then send the listing again.",
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


@pytest.mark.endpoint("listings_issue_history", case="success")
async def test_a_seller_issue_reads_needs_you(api: httpx.AsyncClient, prev: PubWorld) -> None:
    made = await _previewed(prev, [COLOR])
    body = await _read(api, prev, made.listing_id)
    assert body["marketplace"] == "amazon"
    (issue,) = body["issues"]
    assert issue["field"] == "attributes.color"
    assert issue["marketplace_code"] == "90244"
    assert issue["cause"].startswith("Amazon accepts only values from its own list")
    assert issue["marketplace_message"] == (
        "The value provided for 'color' is not one of the allowed values."
    )
    assert issue["action"].startswith("Pick a value from Amazon's list")
    assert (issue["group"], issue["handler"], issue["status"]) == ("content_data", "you", "open")
    assert issue["value_submitted"] == "Navy blue"
    assert body["counts"]["needs_you"] == 1 and body["counts"]["fixed"] == 0
    (attempt,) = body["attempts"]
    assert (attempt["kind"], attempt["outcome"]) == ("precheck", "issues")
    assert attempt["issue_ids"] == [issue["id"]]
    assert body["fixes"] == []


async def test_a_repair_shows_before_and_after(api: httpx.AsyncClient, prev: PubWorld) -> None:
    made = await _previewed(prev, [COLOR])
    issue_id = prev.scalar(
        "SELECT id FROM listing_issues WHERE listing_id = %s AND status = 'open'",
        [made.listing_id],
    )
    prev.conn.execute(
        "INSERT INTO listing_repairs (tenant_id, listing_id, marketplace, field_path, attribute, "
        "rule, issue_key, source_issue_id, source_digest, to_digest, value_json) "
        "SELECT tenant_id, listing_id, marketplace, field_path, 'color', 'enum_fold', dedupe_key, "
        "id, sha256('a'), sha256('b'), "
        '\'[{"value": "Navy", "marketplace_id": "A21TJRUUN4KGV"}]\'::jsonb '
        "FROM listing_issues WHERE id = %s",
        [issue_id],
    )
    body = await _read(api, prev, made.listing_id, REVIEWER)
    (fix,) = body["fixes"]
    assert (fix["kind"], fix["rule"], fix["field"]) == ("repair", "enum_fold", "attributes.color")
    assert (fix["before"], fix["after"]) == ("Navy blue", "Navy")
    assert fix["reason"].startswith("The marketplace accepts only values from its own list")
    assert fix["outcome"] == "waiting"  # nothing was sent after the repair
    assert fix["issue_id"] == str(issue_id)
    (issue,) = body["issues"]
    assert (issue["lane"], issue["handler"]) == ("auto_repair", "kuinn")
    assert body["counts"]["fixed"] == 1 and body["counts"]["fixing"] == 1


async def test_an_unknown_code_reads_staff(api: httpx.AsyncClient, prev: PubWorld) -> None:
    made = await _previewed(prev, [UNKNOWN])
    body = await _read(api, prev, made.listing_id)
    (issue,) = body["issues"]
    assert issue["handler"] == "staff" and issue["group"] == "unknown"
    assert issue["cause"].startswith("Kuinn does not know this marketplace message yet")
    assert issue["marketplace_message"] == "Something unusual went wrong."


@pytest.mark.endpoint("listings_issue_history", case="validation")
async def test_history_bad_uuid_422(api: httpx.AsyncClient, prev: PubWorld) -> None:
    response = await api.get(_url("nope"), headers=prev.world.headers(OPERATOR))
    assert response.status_code == 422


@pytest.mark.endpoint("listings_issue_history", case="missing_auth")
async def test_history_missing_auth_401(api: httpx.AsyncClient, prev: PubWorld) -> None:
    assert (await api.get(_url(uuid.uuid4()))).status_code == 401


async def _invalid(api: httpx.AsyncClient, pw: PubWorld, variant: str) -> None:
    response = await api.get(_url(uuid.uuid4()), headers=bad_bearer(pw.world, variant, OPERATOR))
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "UNAUTHENTICATED"


@pytest.mark.endpoint("listings_issue_history", case="invalid_token", variant="expired")
async def test_history_invalid_token_expired(api: httpx.AsyncClient, prev: PubWorld) -> None:
    await _invalid(api, prev, "expired")


@pytest.mark.endpoint("listings_issue_history", case="invalid_token", variant="bad_signature")
async def test_history_invalid_token_bad_signature(api: httpx.AsyncClient, prev: PubWorld) -> None:
    await _invalid(api, prev, "bad_signature")


@pytest.mark.endpoint("listings_issue_history", case="invalid_token", variant="wrong_typ")
async def test_history_invalid_token_wrong_typ(api: httpx.AsyncClient, prev: PubWorld) -> None:
    await _invalid(api, prev, "wrong_typ")


async def test_history_every_role_reads(api: httpx.AsyncClient, prev: PubWorld) -> None:
    """Every role holds `tenant:read`, so the route has no 403 case."""
    made = await _previewed(prev, [])
    for email in ("owner@acme.test", OPERATOR, REVIEWER, "admin@acme.test"):
        assert (
            await api.get(_url(made.listing_id), headers=prev.world.headers(email))
        ).status_code == 200


@pytest.mark.endpoint("listings_issue_history", case="wrong_tenant")
async def test_history_other_tenant_404(api: httpx.AsyncClient, prev: PubWorld) -> None:
    made = await _previewed(prev, [COLOR])
    response = await api.get(
        _url(made.listing_id), headers=prev.world.headers("operator@bharat.test")
    )
    assert response.status_code == 404
    assert "attributes.color" not in response.text


@pytest.mark.endpoint("listings_issue_history", case="not_found")
async def test_history_not_found_404(api: httpx.AsyncClient, prev: PubWorld) -> None:
    response = await api.get(_url(uuid.uuid4()), headers=prev.world.headers(OPERATOR))
    assert response.status_code == 404
