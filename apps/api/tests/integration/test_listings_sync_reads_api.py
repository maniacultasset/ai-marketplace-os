"""S21 T23, T24 (AC10, AC13, AC14; D3, D7; CP4, CP7): `POST /api/v1/listings/{id}/refresh-status`
and `GET /api/v1/listings/{id}/submissions`, with their case matrices."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from app.services.publish import REFRESH_WINDOW_S
from mkos_schemas.codes import ErrorCode

from .conftest import LOCAL_HOSTS
from .generation_support import set_status
from .products_support import bad_bearer
from .publish_support import PubWorld, approved, pub, publish_and_run, publish_one, retry, run_job
from .review_support import OPERATOR, REVIEWER

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["pub"]
RL = ("Throttled", "t", ErrorCode.RATE_LIMITED)


def _refresh_url(listing_id: Any) -> str:
    return f"/api/v1/listings/{listing_id}/refresh-status"


def _subs_url(listing_id: Any) -> str:
    return f"/api/v1/listings/{listing_id}/submissions"


async def _refresh(
    api: httpx.AsyncClient,
    pub: PubWorld,
    listing_id: Any,
    email: str = OPERATOR,
    key: str | None = None,
) -> httpx.Response:
    return await api.post(_refresh_url(listing_id), headers=pub.world.headers(email, key))


def _syncs(pub: PubWorld, listing_id: Any) -> int:
    return int(
        pub.scalar(
            "SELECT count(*) FROM job_runs WHERE listing_id = %s AND kind = 'listing_status_sync'",
            [listing_id],
        )
    )


# ---- refresh-status ----


@pytest.mark.endpoint("listings_refresh_status", case="success")
async def test_refresh_status_resets_and_enqueues(api: httpx.AsyncClient, pub: PubWorld) -> None:
    made = await approved(pub)
    await publish_and_run(api, pub, made)
    pub.conn.execute("UPDATE listings SET sync_poll_count = 4 WHERE id = %s", [made.listing_id])
    response = await _refresh(api, pub, made.listing_id)
    assert response.status_code == 202, response.text
    job = pub.job(response.json()["job_id"])
    assert (job["kind"], job["created_by"], job["payload_json"]["reason"]) == (
        "listing_status_sync",
        pub.world.user(OPERATOR),
        "refresh",
    )
    row = pub.listing(made.listing_id)
    assert (row["sync_poll_count"], row["next_sync_at"], row["last_event"]) == (
        0,
        None,
        "refresh_requested",
    )
    pub.fake.read_states[
        str(
            pub.scalar(
                "SELECT seller_sku FROM listing_items WHERE listing_id = %s", [made.listing_id]
            )
        )
    ] = "live"
    await run_job(job["id"])
    assert pub.listing(made.listing_id)["marketplace_state"] == "live"


async def test_refresh_status_partial_listing_ok(api: httpx.AsyncClient, pub: PubWorld) -> None:
    made = await approved(pub)
    pub.fake.set_fail_fields({"content.title": RL})
    await publish_and_run(api, pub, made)
    response = await _refresh(api, pub, made.listing_id)
    assert response.status_code == 202, response.text


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("blocked", "marketplace_action_required"),
        ("blocked", "publish_attempts_exhausted"),
        ("failed", "publish_failed"),
    ],
)
async def test_refresh_status_blocked_409(
    api: httpx.AsyncClient, pub: PubWorld, status: str, reason: str
) -> None:
    made = await approved(pub)
    await publish_and_run(api, pub, made)
    set_status(pub.world, made.listing_id, status, reason)
    response = await _refresh(api, pub, made.listing_id)
    assert response.status_code == 409
    assert response.json()["error"]["type"] == "INVALID_STATUS_TRANSITION"
    assert _syncs(pub, made.listing_id) == 0


async def test_refresh_status_never_submitted_409(api: httpx.AsyncClient, pub: PubWorld) -> None:
    made = await approved(pub)
    response = await _refresh(api, pub, made.listing_id)
    assert response.status_code == 409
    assert response.json()["error"]["type"] == "INVALID_STATUS_TRANSITION"


async def test_refresh_status_publishing_409_busy(api: httpx.AsyncClient, pub: PubWorld) -> None:
    made = await approved(pub)
    await publish_one(api, pub, made)
    response = await _refresh(api, pub, made.listing_id)
    assert response.status_code == 409 and response.json()["error"]["type"] == "LISTING_BUSY"


async def test_refresh_status_second_in_5_min_429_then_after_ok(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    made = await approved(pub)
    await publish_and_run(api, pub, made)
    first = await _refresh(api, pub, made.listing_id)
    await run_job(uuid.UUID(first.json()["job_id"]))
    second = await _refresh(api, pub, made.listing_id)
    assert second.status_code == 429 and second.json()["error"]["type"] == "RATE_LIMITED"
    # audit VALIDATION-2: Retry-After is the rest of the 5-minute window, which the server knows
    assert 1 <= int(second.headers["retry-after"]) <= REFRESH_WINDOW_S
    with pub.conn.transaction():  # 200 s of the window pass
        pub.conn.execute("SET LOCAL session_replication_role = replica")
        pub.conn.execute(
            "UPDATE job_runs SET queued_at = queued_at - interval '200 seconds' "
            "WHERE listing_id = %s AND kind = 'listing_status_sync'",
            [made.listing_id],
        )
    later = await _refresh(api, pub, made.listing_id)
    assert later.status_code == 429
    assert 1 <= int(later.headers["retry-after"]) <= REFRESH_WINDOW_S - 200 + 1
    with pub.conn.transaction():  # the clock moves 5 minutes on (terminal rows are immutable)
        pub.conn.execute("SET LOCAL session_replication_role = replica")
        pub.conn.execute(
            "UPDATE job_runs SET queued_at = queued_at - interval '301 seconds' "
            "WHERE listing_id = %s AND kind = 'listing_status_sync'",
            [made.listing_id],
        )
    third = await _refresh(api, pub, made.listing_id)
    assert third.status_code == 202, third.text


async def test_refresh_status_live_sync_job_returned(api: httpx.AsyncClient, pub: PubWorld) -> None:
    made = await approved(pub)
    await publish_and_run(api, pub, made)
    first = await _refresh(api, pub, made.listing_id)
    again = await _refresh(api, pub, made.listing_id)
    assert again.status_code == 202 and again.json()["job_id"] == first.json()["job_id"]


@pytest.mark.endpoint("listings_refresh_status", case="conflict")
async def test_refresh_status_replay_not_429_and_key_reuse_409(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    made = await approved(pub)
    await publish_and_run(api, pub, made)
    key = str(uuid.uuid4())
    first = await _refresh(api, pub, made.listing_id, key=key)
    await run_job(uuid.UUID(first.json()["job_id"]))
    replay = await _refresh(api, pub, made.listing_id, key=key)
    assert replay.status_code == 202 and replay.content == first.content
    other = await _refresh(api, pub, uuid.uuid4(), key=key)
    assert other.status_code == 409 and other.json()["error"]["type"] == "IDEMPOTENCY_KEY_REUSED"


@pytest.mark.endpoint("listings_refresh_status", case="validation")
@pytest.mark.parametrize("variant", ["bad_uuid", "no_key"])
async def test_refresh_status_validation_422(
    api: httpx.AsyncClient, pub: PubWorld, variant: str
) -> None:
    headers = pub.world.headers(OPERATOR)
    url = _refresh_url(uuid.uuid4())
    if variant == "bad_uuid":
        url = _refresh_url("nope")
    else:
        headers.pop("Idempotency-Key")
    assert (await api.post(url, headers=headers)).status_code == 422


@pytest.mark.endpoint("listings_refresh_status", case="missing_auth")
async def test_refresh_status_missing_auth_401(api: httpx.AsyncClient, pub: PubWorld) -> None:
    response = await api.post(_refresh_url(uuid.uuid4()), headers={"Idempotency-Key": "k"})
    assert response.status_code == 401


async def _invalid(
    api: httpx.AsyncClient, pub: PubWorld, variant: str, method: str, url: str
) -> None:
    headers = {**bad_bearer(pub.world, variant, OPERATOR), "Idempotency-Key": str(uuid.uuid4())}
    response = await api.request(method, url, headers=headers)
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "UNAUTHENTICATED"


@pytest.mark.endpoint("listings_refresh_status", case="invalid_token", variant="expired")
async def test_refresh_status_invalid_token_expired(api: httpx.AsyncClient, pub: PubWorld) -> None:
    await _invalid(api, pub, "expired", "POST", _refresh_url(uuid.uuid4()))


@pytest.mark.endpoint("listings_refresh_status", case="invalid_token", variant="bad_signature")
async def test_refresh_status_invalid_token_bad_signature(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    await _invalid(api, pub, "bad_signature", "POST", _refresh_url(uuid.uuid4()))


@pytest.mark.endpoint("listings_refresh_status", case="invalid_token", variant="wrong_typ")
async def test_refresh_status_invalid_token_wrong_typ(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    await _invalid(api, pub, "wrong_typ", "POST", _refresh_url(uuid.uuid4()))


@pytest.mark.endpoint("listings_refresh_status", case="wrong_role")
@pytest.mark.parametrize("email", [REVIEWER, "admin@acme.test"])
async def test_refresh_status_wrong_role_403(
    api: httpx.AsyncClient, pub: PubWorld, email: str
) -> None:
    response = await _refresh(api, pub, uuid.uuid4(), email)
    assert response.status_code == 403


@pytest.mark.endpoint("listings_refresh_status", case="wrong_tenant")
async def test_refresh_status_wrong_tenant_404(api: httpx.AsyncClient, pub: PubWorld) -> None:
    made = await approved(pub)
    await publish_and_run(api, pub, made)
    response = await _refresh(api, pub, made.listing_id, "operator@bharat.test")
    assert response.status_code == 404
    assert _syncs(pub, made.listing_id) == 0


@pytest.mark.endpoint("listings_refresh_status", case="not_found")
async def test_refresh_status_not_found_404(api: httpx.AsyncClient, pub: PubWorld) -> None:
    assert (await _refresh(api, pub, uuid.uuid4())).status_code == 404


# ---- submissions ----


@pytest.mark.endpoint("listings_submissions", case="success")
async def test_submissions_per_attempt_fields(api: httpx.AsyncClient, pub: PubWorld) -> None:
    made = await approved(pub)
    pub.fake.set_fail_fields({"content.title": RL})
    await publish_and_run(api, pub, made)
    pub.fake.set_fail_fields({})
    response = await retry(api, pub, made)
    await run_job(uuid.UUID(response.json()["job_id"]))
    page = await api.get(_subs_url(made.listing_id), headers=pub.world.headers(REVIEWER))
    assert page.status_code == 200, page.text
    body = page.json()
    assert [s["attempt"] for s in body["data"]] == [2, 1]
    second, first = body["data"]
    assert (second["operation"], second["field_paths"], second["outcome"]) == (
        "patch",
        ["content.title"],
        "accepted",
    )
    assert second["retry_justification"] == "retryable_condition"
    failed = [f for f in first["items"][0]["fields"] if f["status"] == "failed"]
    assert [f["field"] for f in failed] == ["content.title"]
    assert failed[0]["error"]["error_code"] == "RATE_LIMITED"
    assert first["error"]["error_code"] == "API_REJECTED_PARTIAL"


async def test_submissions_no_storage_key_or_raw_body(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    made = await approved(pub)
    await publish_and_run(api, pub, made)
    text = (await api.get(_subs_url(made.listing_id), headers=pub.world.headers(OPERATOR))).text
    for banned in (
        "payload_storage_key",
        "submissions/",
        "idempotency_key",
        "sha256",
        "raw_response",
        "facts_digest",
    ):
        assert banned not in text, banned


async def test_submissions_two_pages_keyset(api: httpx.AsyncClient, pub: PubWorld) -> None:
    made = await approved(pub)
    pub.fake.set_fail_fields({"content.title": RL})
    await publish_and_run(api, pub, made)
    pub.fake.set_fail_fields({})
    response = await retry(api, pub, made)
    await run_job(uuid.UUID(response.json()["job_id"]))
    headers = pub.world.headers(OPERATOR)
    first = (await api.get(_subs_url(made.listing_id), headers=headers, params={"limit": 1})).json()
    assert first["page"]["has_more"] and first["page"]["limit"] == 1
    second = (
        await api.get(
            _subs_url(made.listing_id),
            headers=headers,
            params={"limit": 1, "cursor": first["page"]["next_cursor"]},
        )
    ).json()
    assert [s["attempt"] for s in first["data"] + second["data"]] == [2, 1]
    assert second["page"]["has_more"] is False and second["page"]["next_cursor"] is None


@pytest.mark.endpoint("listings_submissions", case="validation")
@pytest.mark.parametrize("variant", ["bad_uuid", "limit_0", "limit_201", "bad_cursor"])
async def test_submissions_validation_422(
    api: httpx.AsyncClient, pub: PubWorld, variant: str
) -> None:
    url = _subs_url(uuid.uuid4())
    params: dict[str, Any] = {}
    if variant == "bad_uuid":
        url = _subs_url("nope")
    elif variant == "limit_0":
        params = {"limit": 0}
    elif variant == "limit_201":
        params = {"limit": 201}
    else:
        params = {"cursor": "tampered!!"}
    response = await api.get(url, headers=pub.world.headers(OPERATOR), params=params)
    assert response.status_code == 422


@pytest.mark.endpoint("listings_submissions", case="missing_auth")
async def test_submissions_missing_auth_401(api: httpx.AsyncClient, pub: PubWorld) -> None:
    assert (await api.get(_subs_url(uuid.uuid4()))).status_code == 401


@pytest.mark.endpoint("listings_submissions", case="invalid_token", variant="expired")
async def test_submissions_invalid_token_expired(api: httpx.AsyncClient, pub: PubWorld) -> None:
    await _invalid(api, pub, "expired", "GET", _subs_url(uuid.uuid4()))


@pytest.mark.endpoint("listings_submissions", case="invalid_token", variant="bad_signature")
async def test_submissions_invalid_token_bad_signature(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    await _invalid(api, pub, "bad_signature", "GET", _subs_url(uuid.uuid4()))


@pytest.mark.endpoint("listings_submissions", case="invalid_token", variant="wrong_typ")
async def test_submissions_invalid_token_wrong_typ(api: httpx.AsyncClient, pub: PubWorld) -> None:
    await _invalid(api, pub, "wrong_typ", "GET", _subs_url(uuid.uuid4()))


async def test_submissions_na_wrong_role_all_roles_200(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    made = await approved(pub)
    for email in ("owner@acme.test", OPERATOR, REVIEWER, "admin@acme.test"):
        response = await api.get(_subs_url(made.listing_id), headers=pub.world.headers(email))
        assert response.status_code == 200, email


@pytest.mark.endpoint("listings_submissions", case="wrong_tenant")
async def test_submissions_bharat_404(api: httpx.AsyncClient, pub: PubWorld) -> None:
    made = await approved(pub)
    await publish_and_run(api, pub, made)
    response = await api.get(
        _subs_url(made.listing_id), headers=pub.world.headers("operator@bharat.test")
    )
    assert response.status_code == 404


@pytest.mark.endpoint("listings_submissions", case="not_found")
async def test_submissions_not_found_404(api: httpx.AsyncClient, pub: PubWorld) -> None:
    response = await api.get(_subs_url(uuid.uuid4()), headers=pub.world.headers(OPERATOR))
    assert response.status_code == 404


async def test_publish_no_leak(api: httpx.AsyncClient, pub: PubWorld) -> None:
    """No presigned URL or token reaches a submission row, report, field result, issue or the
    submissions response (the FakeAdapter is handed URLs only in memory)."""
    made = await approved(pub)
    pub.fake.set_fail_fields({"content.title": RL})
    await publish_and_run(api, pub, made)
    dumps = [
        str(r)
        for r in pub.rows(
            "SELECT s.*, j.result_json, j.error_report_json FROM listing_submissions s "
            "JOIN job_runs j ON j.id = s.job_id WHERE s.listing_id = %s",
            [made.listing_id],
        )
    ]
    dumps += [
        str(r)
        for r in pub.rows(
            "SELECT * FROM publish_field_results WHERE listing_id = %s", [made.listing_id]
        )
    ]
    dumps += [
        str(r)
        for r in pub.rows("SELECT * FROM listing_issues WHERE listing_id = %s", [made.listing_id])
    ]
    dumps.append(
        (await api.get(_subs_url(made.listing_id), headers=pub.world.headers(OPERATOR))).text
    )
    for text in dumps:
        assert "X-Amz-Signature" not in text and "CANARY" not in text and "https://" not in text
