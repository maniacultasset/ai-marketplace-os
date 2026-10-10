"""S22 T20 (AC17; §3.1): `POST /api/v1/listings/publish` for Flipkart listings with
`FLIPKART_ENABLED=true`: the S21 case matrix holds for Flipkart as it does for Amazon."""

from __future__ import annotations

import uuid

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .flipkart_publish_support import FkWorld, fkp, pub
from .publish_support import approved, post_publish
from .review_support import OPERATOR, OWNER, REVIEWER, Made, generated

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["fkp", "pub"]


async def _fk(fk: FkWorld) -> Made:
    return await approved(fk.pub, marketplace="flipkart")


@pytest.mark.endpoint("listings_publish", case="success", variant="flipkart")
async def test_listings_publish_flipkart_success_202(api: httpx.AsyncClient, fkp: FkWorld) -> None:
    made = await _fk(fkp)
    response = await post_publish(api, fkp.pub, OWNER, [{"listing_id": str(made.listing_id)}])
    assert response.status_code == 202, response.text
    (item,) = response.json()["items"]
    assert (item["http_status"], item["listing_status"], item["error"]) == (202, "publishing", None)
    job = fkp.pub.job(item["job_id"])
    assert (job["kind"], job["queue"]) == ("listing_publish", "publish")
    assert fkp.calls() == [] and fkp.opened == []  # invariant 1: only the job publishes


@pytest.mark.endpoint("listings_publish", case="validation", variant="flipkart")
async def test_listings_publish_flipkart_validation_422(
    api: httpx.AsyncClient, fkp: FkWorld
) -> None:
    made = await _fk(fkp)
    response = await api.post(
        "/api/v1/listings/publish",
        headers=fkp.world.headers(OPERATOR),
        json={"items": [{"listing_id": str(made.listing_id), "marketplace": "flipkart"}]},
    )
    assert response.status_code == 422, response.text
    assert fkp.jobs(made.listing_id) == 0


@pytest.mark.endpoint("listings_publish", case="missing_auth", variant="flipkart")
async def test_listings_publish_flipkart_missing_auth_401(
    api: httpx.AsyncClient, fkp: FkWorld
) -> None:
    made = await _fk(fkp)
    response = await api.post(
        "/api/v1/listings/publish",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={"items": [{"listing_id": str(made.listing_id)}]},
    )
    assert response.status_code == 401
    assert fkp.jobs(made.listing_id) == 0


@pytest.mark.endpoint("listings_publish", case="wrong_role", variant="flipkart")
async def test_listings_publish_flipkart_wrong_role_403(
    api: httpx.AsyncClient, fkp: FkWorld
) -> None:
    made = await _fk(fkp)
    response = await post_publish(api, fkp.pub, REVIEWER, [{"listing_id": str(made.listing_id)}])
    assert response.status_code == 403
    assert fkp.jobs(made.listing_id) == 0


@pytest.mark.endpoint("listings_publish", case="wrong_tenant", variant="flipkart")
async def test_listings_publish_flipkart_wrong_tenant_item_404(
    api: httpx.AsyncClient, fkp: FkWorld
) -> None:
    made = await _fk(fkp)
    response = await post_publish(
        api, fkp.pub, "operator@bharat.test", [{"listing_id": str(made.listing_id)}]
    )
    assert response.status_code == 202
    (item,) = response.json()["items"]
    assert (item["http_status"], item["error"]["error_code"]) == (404, "NOT_FOUND")
    assert fkp.pub.listing(made.listing_id)["status"] == "approved"


@pytest.mark.endpoint("listings_publish", case="not_found", variant="flipkart")
async def test_listings_publish_flipkart_not_found_item_404(
    api: httpx.AsyncClient, fkp: FkWorld
) -> None:
    response = await post_publish(api, fkp.pub, OPERATOR, [{"listing_id": str(uuid.uuid4())}])
    (item,) = response.json()["items"]
    assert (item["http_status"], item["error"]["error_code"]) == (404, "NOT_FOUND")


@pytest.mark.endpoint("listings_publish", case="conflict", variant="flipkart")
async def test_listings_publish_flipkart_conflict(api: httpx.AsyncClient, fkp: FkWorld) -> None:
    """Key reuse with another body 409; a Flipkart listing never approved is an item 409."""
    made = await _fk(fkp)
    key = str(uuid.uuid4())
    body = [{"listing_id": str(made.listing_id)}]
    first = await post_publish(api, fkp.pub, OPERATOR, body, key=key)
    again = await post_publish(api, fkp.pub, OPERATOR, body, key=key)
    assert first.status_code == again.status_code == 202 and first.content == again.content
    other = await post_publish(api, fkp.pub, OPERATOR, [{"listing_id": str(uuid.uuid4())}], key=key)
    assert other.status_code == 409 and other.json()["error"]["type"] == "IDEMPOTENCY_KEY_REUSED"
    fresh = await generated(fkp.world, marketplace="flipkart")
    response = await post_publish(api, fkp.pub, OPERATOR, [{"listing_id": str(fresh.listing_id)}])
    (item,) = response.json()["items"]
    assert (item["http_status"], item["error"]["error_code"]) == (409, "INVALID_STATUS_TRANSITION")
