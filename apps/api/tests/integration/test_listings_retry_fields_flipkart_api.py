"""S22 T20, T22 (AC17, AC18; §3.1): `POST /api/v1/listings/{id}/publish/retry-fields` for a
Flipkart listing with `FLIPKART_ENABLED=true`, after a partial publish through the real
`FlipkartAdapter` on the in-process fake: the S21 case matrix holds for Flipkart."""

from __future__ import annotations

import uuid

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .flipkart_publish_support import FkWorld, fkp, pub
from .publish_support import approved, post_publish, retry, run_job
from .review_support import OPERATOR, REVIEWER, Made

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["fkp", "pub"]
QUOTA = ("FAKE-FK-QUOTA", "Request throttled")


async def _partial(api: httpx.AsyncClient, fk: FkWorld) -> Made:
    """A standalone Flipkart listing whose `title` was refused (retryable): failed/partial."""
    made = await approved(fk.pub, marketplace="flipkart")
    fk.app.state.fake.fk_reject_attribute["title"] = QUOTA
    response = await post_publish(api, fk.pub, OPERATOR, [{"listing_id": str(made.listing_id)}])
    (item,) = response.json()["items"]
    await run_job(uuid.UUID(item["job_id"]))
    fk.app.state.fake.fk_reject_attribute.clear()
    row = fk.pub.listing(made.listing_id)
    assert (row["status"], row["status_reason"]) == ("failed", "publish_partial"), row
    return made


def _url(listing_id: uuid.UUID | str) -> str:
    return f"/api/v1/listings/{listing_id}/publish/retry-fields"


@pytest.mark.endpoint("listings_retry_fields", case="success", variant="flipkart")
async def test_listings_retry_fields_flipkart_success_202(
    api: httpx.AsyncClient, fkp: FkWorld
) -> None:
    made = await _partial(api, fkp)
    response = await retry(api, fkp.pub, made)
    assert response.status_code == 202, response.text
    job = fkp.pub.job(response.json()["job_id"])
    assert (job["kind"], job["status"]) == ("listing_publish", "queued")
    assert fkp.pub.listing(made.listing_id)["status"] == "publishing"


@pytest.mark.endpoint("listings_retry_fields", case="validation", variant="flipkart")
async def test_listings_retry_fields_flipkart_validation_422(
    api: httpx.AsyncClient, fkp: FkWorld
) -> None:
    response = await api.post(_url("not-a-uuid"), headers=fkp.world.headers(OPERATOR))
    assert response.status_code == 422


@pytest.mark.endpoint("listings_retry_fields", case="missing_auth", variant="flipkart")
async def test_listings_retry_fields_flipkart_missing_auth_401(
    api: httpx.AsyncClient, fkp: FkWorld
) -> None:
    made = await _partial(api, fkp)
    response = await api.post(_url(made.listing_id), headers={"Idempotency-Key": str(uuid.uuid4())})
    assert response.status_code == 401
    assert fkp.jobs(made.listing_id) == 1


@pytest.mark.endpoint("listings_retry_fields", case="wrong_role", variant="flipkart")
async def test_listings_retry_fields_flipkart_wrong_role_403(
    api: httpx.AsyncClient, fkp: FkWorld
) -> None:
    made = await _partial(api, fkp)
    response = await retry(api, fkp.pub, made, REVIEWER)
    assert response.status_code == 403
    assert fkp.jobs(made.listing_id) == 1


@pytest.mark.endpoint("listings_retry_fields", case="wrong_tenant", variant="flipkart")
async def test_listings_retry_fields_flipkart_wrong_tenant_404(
    api: httpx.AsyncClient, fkp: FkWorld
) -> None:
    made = await _partial(api, fkp)
    response = await retry(api, fkp.pub, made, "operator@bharat.test")
    assert response.status_code == 404
    assert fkp.jobs(made.listing_id) == 1


@pytest.mark.endpoint("listings_retry_fields", case="not_found", variant="flipkart")
async def test_listings_retry_fields_flipkart_not_found_404(
    api: httpx.AsyncClient, fkp: FkWorld
) -> None:
    response = await api.post(_url(uuid.uuid4()), headers=fkp.world.headers(OPERATOR))
    assert response.status_code == 404


@pytest.mark.endpoint("listings_retry_fields", case="conflict", variant="flipkart")
async def test_listings_retry_fields_flipkart_conflict_409(
    api: httpx.AsyncClient, fkp: FkWorld
) -> None:
    """Key reuse on another listing 409; an approved Flipkart listing never published 409."""
    made = await _partial(api, fkp)
    key = str(uuid.uuid4())
    first = await retry(api, fkp.pub, made, key=key)
    replay = await retry(api, fkp.pub, made, key=key)
    assert first.status_code == replay.status_code == 202 and first.content == replay.content
    other = await api.post(_url(uuid.uuid4()), headers=fkp.world.headers(OPERATOR, key))
    assert other.status_code == 409 and other.json()["error"]["type"] == "IDEMPOTENCY_KEY_REUSED"
    fresh = await approved(fkp.pub, marketplace="flipkart")
    response = await retry(api, fkp.pub, fresh)
    assert response.status_code == 409
    assert response.json()["error"]["type"] == "INVALID_STATUS_TRANSITION"
