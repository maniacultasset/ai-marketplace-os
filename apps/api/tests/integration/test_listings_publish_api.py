"""S21 T9-T11 (AC1, AC4, AC5, AC13; DW1, DW3; D2, D4, D6): `POST /api/v1/listings/publish` and
`POST /api/v1/listings/{id}/publish/retry-fields`: the gate, idempotency and the case matrix."""

from __future__ import annotations

import socket
import uuid
from typing import Any

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .generation_support import set_status
from .products_support import bad_bearer
from .publish_support import (
    PUBLISH_URL,
    PubWorld,
    approved,
    post_publish,
    pub,
    publish_and_run,
    publish_one,
    retry,
    run_job,
)
from .review_support import OPERATOR, OWNER, REVIEWER, generated

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["pub"]


def _jobs(pub: PubWorld, listing_id: uuid.UUID) -> int:
    return int(
        pub.scalar(
            "SELECT count(*) FROM job_runs WHERE listing_id = %s AND kind = 'listing_publish'",
            [listing_id],
        )
    )


@pytest.mark.endpoint("listings_publish", case="success")
async def test_listings_publish_success_202(api: httpx.AsyncClient, pub: PubWorld) -> None:
    made = await approved(pub)
    response = await post_publish(
        api,
        pub,
        OWNER,
        [{"listing_id": str(made.listing_id), "listing_version_id": str(made.version_id)}],
    )
    assert response.status_code == 202, response.text
    body = response.json()
    (item,) = body["items"]
    assert (item["http_status"], item["listing_status"], item["error"]) == (202, "publishing", None)
    assert body["poll_url"] == f"/api/v1/jobs/{item['job_id']}"
    batch = pub.rows("SELECT kind, item_count FROM batches WHERE id = %s", [body["batch_id"]])
    assert batch == [("publish", 1)]
    audit = pub.scalar(
        "SELECT count(*) FROM audit_log WHERE entity_id = %s AND action = "
        "'listing.publish_requested'",
        [made.listing_id],
    )
    assert audit == 1
    assert pub.listing(made.listing_id)["last_event"] == "publish"


@pytest.mark.parametrize("status", ["validated", "needs_review"])
async def test_publish_gate_not_approved(
    api: httpx.AsyncClient, pub: PubWorld, status: str
) -> None:
    made = await generated(pub.world)
    if status == "needs_review":
        set_status(pub.world, made.listing_id, status)
    item = await publish_one(api, pub, made)
    assert (item["http_status"], item["error"]["error_code"]) == (409, "INVALID_STATUS_TRANSITION")
    assert _jobs(pub, made.listing_id) == 0


async def test_publish_gate_stale_version(api: httpx.AsyncClient, pub: PubWorld) -> None:
    made = await approved(pub)
    response = await post_publish(
        api,
        pub,
        OPERATOR,
        [{"listing_id": str(made.listing_id), "listing_version_id": str(uuid.uuid4())}],
    )
    (item,) = response.json()["items"]
    assert (item["http_status"], item["error"]["error_code"]) == (409, "STALE_VERSION")


async def test_publish_gate_busy_and_race(api: httpx.AsyncClient, pub: PubWorld) -> None:
    """DW6 at the API: a second publish (another key) of a publishing listing is LISTING_BUSY."""
    made = await approved(pub)
    first = await publish_one(api, pub, made)
    second = await publish_one(api, pub, made)
    assert first["http_status"] == 202
    assert (second["http_status"], second["error"]["error_code"]) == (409, "LISTING_BUSY")
    assert _jobs(pub, made.listing_id) == 1


async def test_publish_gate_paused(api: httpx.AsyncClient, pub: PubWorld) -> None:
    made = await approved(pub)
    pub.conn.execute("UPDATE listings SET paused_at = now() WHERE id = %s", [made.listing_id])
    item = await publish_one(api, pub, made)
    assert (item["http_status"], item["error"]["error_code"]) == (409, "LISTING_PAUSED")


async def test_publish_no_account_409_no_job(api: httpx.AsyncClient, pub: PubWorld) -> None:
    made = await approved(pub)
    pub.conn.execute(
        "UPDATE marketplace_accounts SET status = 'needs_reconnect', needs_reconnect_since = now() "
        "WHERE id = %s",
        [pub.accounts["acme"]],
    )
    item = await publish_one(api, pub, made)
    assert (item["http_status"], item["error"]["error_code"]) == (409, "AUTH_EXPIRED")
    assert _jobs(pub, made.listing_id) == 0


async def test_publish_gate_db_backstop(
    api: httpx.AsyncClient, pub: PubWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the API checks patched out, the 0004 publish gate still refuses: item 409, no job."""
    from app.services import publish

    made = await generated(pub.world)  # validated, never approved

    async def no_checks(uow: Any, listing: Any, **kw: Any) -> Any:
        return publish.Plan(event="publish", mode="publish", parent_job_id=None)

    monkeypatch.setattr(publish, "_gate", lambda listing, version: listing)
    monkeypatch.setattr(publish, "plan_publish", no_checks)
    item = await publish_one(api, pub, made)
    assert (item["http_status"], item["error"]["error_code"]) == (409, "INVALID_STATUS_TRANSITION")
    assert _jobs(pub, made.listing_id) == 0


async def test_publish_batch_mixed_items(api: httpx.AsyncClient, pub: PubWorld) -> None:
    ok = await approved(pub)
    not_approved = await generated(pub.world)
    response = await post_publish(
        api,
        pub,
        OPERATOR,
        [
            {"listing_id": str(ok.listing_id)},
            {"listing_id": str(not_approved.listing_id)},
            {"listing_id": str(uuid.uuid4())},
        ],
    )
    assert response.status_code == 202
    assert [i["http_status"] for i in response.json()["items"]] == [202, 409, 404]


async def test_publish_partial_listing_is_retry_fields(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    """D2: publishing a `failed/publish_partial` listing resends only the failed fields."""
    from mkos_schemas.codes import ErrorCode

    made = await approved(pub)
    pub.fake.set_fail_fields({"content.title": ("T", "t", ErrorCode.RATE_LIMITED)})
    await publish_and_run(api, pub, made)
    pub.fake.set_fail_fields({})
    item = await publish_and_run(api, pub, made)
    payload = pub.job(item["job_id"])["payload_json"]
    assert payload["mode"] == "retry_fields"
    subs = pub.submissions(made.listing_id)
    assert (subs[1]["operation"], subs[1]["field_paths"]) == ("patch", ["content.title"])
    assert pub.fake.calls[-1][0] == "update"


@pytest.mark.endpoint("listings_publish", case="validation")
@pytest.mark.parametrize(
    "variant", ["no_items", "201_items", "bad_uuid", "dup_ids", "extra_key", "no_key"]
)
async def test_listings_publish_validation_422(
    api: httpx.AsyncClient, pub: PubWorld, variant: str
) -> None:
    one = {"listing_id": str(uuid.uuid4())}
    body: dict[str, Any] = {"items": [one]}
    headers = pub.world.headers(OPERATOR)
    if variant == "no_items":
        body = {"items": []}
    elif variant == "201_items":
        body = {"items": [{"listing_id": str(uuid.uuid4())} for _ in range(201)]}
    elif variant == "bad_uuid":
        body = {"items": [{"listing_id": "not-a-uuid"}]}
    elif variant == "dup_ids":
        body = {"items": [one, one]}
    elif variant == "extra_key":
        body = {"items": [{**one, "attempt": 2}]}
    else:
        headers.pop("Idempotency-Key")
    response = await api.post(PUBLISH_URL, headers=headers, json=body)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["type"] == "VALIDATION_FAILED"
    assert pub.scalar("SELECT count(*) FROM batches WHERE kind = 'publish'") == 0


@pytest.mark.endpoint("listings_publish", case="missing_auth")
async def test_listings_publish_missing_auth_401(api: httpx.AsyncClient, pub: PubWorld) -> None:
    response = await api.post(PUBLISH_URL, headers={"Idempotency-Key": "k-1"}, json={"items": []})
    assert response.status_code == 401


async def _invalid(
    api: httpx.AsyncClient, pub: PubWorld, variant: str, url: str = PUBLISH_URL
) -> None:
    headers = {**bad_bearer(pub.world, variant, OPERATOR), "Idempotency-Key": str(uuid.uuid4())}
    response = await api.post(url, headers=headers, json={"items": []})
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "UNAUTHENTICATED"


@pytest.mark.endpoint("listings_publish", case="invalid_token", variant="expired")
async def test_listings_publish_invalid_token_expired(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    await _invalid(api, pub, "expired")


@pytest.mark.endpoint("listings_publish", case="invalid_token", variant="bad_signature")
async def test_listings_publish_invalid_token_bad_signature(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    await _invalid(api, pub, "bad_signature")


@pytest.mark.endpoint("listings_publish", case="invalid_token", variant="wrong_typ")
async def test_listings_publish_invalid_token_wrong_typ(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    await _invalid(api, pub, "wrong_typ")


@pytest.mark.endpoint("listings_publish", case="wrong_role")
@pytest.mark.parametrize("email", [REVIEWER, "admin@acme.test"])
async def test_listings_publish_wrong_role_403(
    api: httpx.AsyncClient, pub: PubWorld, email: str
) -> None:
    made = await approved(pub)
    response = await post_publish(api, pub, email, [{"listing_id": str(made.listing_id)}])
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "PERMISSION_DENIED"
    assert _jobs(pub, made.listing_id) == 0


@pytest.mark.endpoint("listings_publish", case="wrong_tenant")
async def test_listings_publish_wrong_tenant_item_404(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    made = await approved(pub)
    response = await post_publish(
        api, pub, "operator@bharat.test", [{"listing_id": str(made.listing_id)}]
    )
    assert response.status_code == 202
    (item,) = response.json()["items"]
    assert (item["http_status"], item["error"]["error_code"]) == (404, "NOT_FOUND")
    assert pub.listing(made.listing_id)["status"] == "approved"


@pytest.mark.endpoint("listings_publish", case="not_found")
async def test_listings_publish_not_found_item_404(api: httpx.AsyncClient, pub: PubWorld) -> None:
    response = await post_publish(api, pub, OPERATOR, [{"listing_id": str(uuid.uuid4())}])
    (item,) = response.json()["items"]
    assert item["http_status"] == 404


@pytest.mark.endpoint("listings_publish", case="conflict")
async def test_publish_idempotency(api: httpx.AsyncClient, pub: PubWorld) -> None:
    """DW3: same key + body replays byte-identically (one batch, one job); another body 409."""
    made = await approved(pub)
    key = str(uuid.uuid4())
    body = [{"listing_id": str(made.listing_id)}]
    first = await post_publish(api, pub, OPERATOR, body, key=key)
    again = await post_publish(api, pub, OPERATOR, body, key=key)
    assert first.status_code == again.status_code == 202
    assert first.content == again.content
    assert pub.scalar("SELECT count(*) FROM batches WHERE kind = 'publish'") == 1
    assert _jobs(pub, made.listing_id) == 1
    other = await post_publish(api, pub, OPERATOR, [{"listing_id": str(uuid.uuid4())}], key=key)
    assert other.status_code == 409
    assert other.json()["error"]["type"] == "IDEMPOTENCY_KEY_REUSED"


async def test_publish_never_calls_adapter_in_request(
    api: httpx.AsyncClient, pub: PubWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = await approved(pub)
    item = await publish_one(api, pub, made)
    assert item["http_status"] == 202 and pub.fake.calls == [] and pub.sinks == []


# ---- retry-fields (T11) ----


async def _partial(api: httpx.AsyncClient, pub: PubWorld) -> Any:
    from mkos_schemas.codes import ErrorCode

    made = await approved(pub)
    pub.fake.set_fail_fields({"content.title": ("T", "t", ErrorCode.RATE_LIMITED)})
    await publish_and_run(api, pub, made)
    pub.fake.set_fail_fields({})
    return made


@pytest.mark.endpoint("listings_retry_fields", case="success")
async def test_listings_retry_fields_success_202(api: httpx.AsyncClient, pub: PubWorld) -> None:
    made = await _partial(api, pub)
    response = await retry(api, pub, made)
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["listing_id"] == str(made.listing_id) and body["status"] == "queued"
    job = pub.job(body["job_id"])
    assert job["payload_json"]["mode"] == "retry_fields" and job["parent_job_id"] is not None
    await run_job(uuid.UUID(body["job_id"]))
    assert pub.listing(made.listing_id)["status"] == "published"


async def test_retry_fields_second_retry_parent_is_latest_job(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    from mkos_schemas.codes import ErrorCode

    made = await _partial(api, pub)
    pub.fake.set_fail_fields({"content.title": ("T", "t", ErrorCode.RATE_LIMITED)})
    first = await retry(api, pub, made)
    await run_job(uuid.UUID(first.json()["job_id"]))
    pub.fake.set_fail_fields({})
    second = await retry(api, pub, made)
    assert second.status_code == 202, second.text
    assert pub.job(second.json()["job_id"])["parent_job_id"] == uuid.UUID(first.json()["job_id"])


@pytest.mark.endpoint("listings_retry_fields", case="validation")
@pytest.mark.parametrize("variant", ["bad_uuid", "no_key"])
async def test_listings_retry_fields_validation_422(
    api: httpx.AsyncClient, pub: PubWorld, variant: str
) -> None:
    headers = pub.world.headers(OPERATOR)
    url = f"/api/v1/listings/{uuid.uuid4()}/publish/retry-fields"
    if variant == "bad_uuid":
        url = "/api/v1/listings/nope/publish/retry-fields"
    else:
        headers.pop("Idempotency-Key")
    response = await api.post(url, headers=headers)
    assert response.status_code == 422


@pytest.mark.endpoint("listings_retry_fields", case="missing_auth")
async def test_listings_retry_fields_missing_auth_401(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    response = await api.post(
        f"/api/v1/listings/{uuid.uuid4()}/publish/retry-fields", headers={"Idempotency-Key": "k"}
    )
    assert response.status_code == 401


def _retry_url() -> str:
    return f"/api/v1/listings/{uuid.uuid4()}/publish/retry-fields"


@pytest.mark.endpoint("listings_retry_fields", case="invalid_token", variant="expired")
async def test_listings_retry_fields_invalid_token_expired(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    await _invalid(api, pub, "expired", _retry_url())


@pytest.mark.endpoint("listings_retry_fields", case="invalid_token", variant="bad_signature")
async def test_listings_retry_fields_invalid_token_bad_signature(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    await _invalid(api, pub, "bad_signature", _retry_url())


@pytest.mark.endpoint("listings_retry_fields", case="invalid_token", variant="wrong_typ")
async def test_listings_retry_fields_invalid_token_wrong_typ(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    await _invalid(api, pub, "wrong_typ", _retry_url())


@pytest.mark.endpoint("listings_retry_fields", case="wrong_role")
@pytest.mark.parametrize("email", [REVIEWER, "admin@acme.test"])
async def test_listings_retry_fields_wrong_role_403(
    api: httpx.AsyncClient, pub: PubWorld, email: str
) -> None:
    response = await retry(api, pub, await approved(pub), email)
    assert response.status_code == 403


@pytest.mark.endpoint("listings_retry_fields", case="wrong_tenant")
async def test_listings_retry_fields_wrong_tenant_404(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    made = await _partial(api, pub)
    response = await retry(api, pub, made, "operator@bharat.test")
    assert response.status_code == 404


@pytest.mark.endpoint("listings_retry_fields", case="not_found")
async def test_listings_retry_fields_not_found_404(api: httpx.AsyncClient, pub: PubWorld) -> None:
    response = await api.post(_retry_url(), headers=pub.world.headers(OPERATOR))
    assert response.status_code == 404


@pytest.mark.endpoint("listings_retry_fields", case="conflict")
async def test_listings_retry_fields_conflict_409(api: httpx.AsyncClient, pub: PubWorld) -> None:
    """Key reuse with another listing 409; an approved (never published) listing 409; a replay
    of the same key returns the one job."""
    made = await _partial(api, pub)
    key = str(uuid.uuid4())
    first = await retry(api, pub, made, key=key)
    replay = await retry(api, pub, made, key=key)
    assert first.status_code == replay.status_code == 202 and first.content == replay.content
    other = await api.post(
        f"/api/v1/listings/{uuid.uuid4()}/publish/retry-fields",
        headers=pub.world.headers(OPERATOR, key),
    )
    assert other.status_code == 409 and other.json()["error"]["type"] == "IDEMPOTENCY_KEY_REUSED"
    fresh = await approved(pub)
    response = await retry(api, pub, fresh)
    assert response.status_code == 409
    assert response.json()["error"]["type"] == "INVALID_STATUS_TRANSITION"


async def test_retry_fields_busy_409(api: httpx.AsyncClient, pub: PubWorld) -> None:
    made = await approved(pub)
    await publish_one(api, pub, made)
    response = await retry(api, pub, made)
    assert response.status_code == 409 and response.json()["error"]["type"] == "LISTING_BUSY"


async def test_socket_blocked_publish_still_202(
    api: httpx.AsyncClient, pub: PubWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant 1: the request opens no outbound socket (the job does the marketplace I/O)."""
    made = await approved(pub)
    real = socket.socket.connect
    targets: list[Any] = []

    def spy(self: socket.socket, address: Any) -> Any:
        targets.append(address)
        return real(self, address)

    monkeypatch.setattr(socket.socket, "connect", spy)
    item = await publish_one(api, pub, made)
    assert item["http_status"] == 202
    assert all(str(t[0]) in LOCAL_HOSTS for t in targets if isinstance(t, tuple))
