"""SI13 AC3 attack (tester): the recategorise of a rejected listing keeps `failed` and its old
approval until the seller regenerates (DEVIATIONS T8). Neither publish path may resend that
pre-recategorise version: both are refused at `facts.category` (the version pins the old
category), before the duplicate-submission check, so even a justified retry resends nothing."""

from __future__ import annotations

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .publish_support import PubWorld, post_publish, pub, retry
from .review_support import OPERATOR
from .test_recategorize_after_rejection import _post, _rejected

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["pub"]


async def test_publish_after_recategorize_resends_nothing(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    made, product, _ = await _rejected(api, pub)
    assert (await _post(api, pub, product)).status_code == 200
    calls = len(pub.fake.calls)
    response = await post_publish(api, pub, OPERATOR, [{"listing_id": str(made.listing_id)}])
    (item,) = response.json()["items"]
    assert item["http_status"] == 409, item
    assert item["error"]["error_code"] == "INVALID_STATUS_TRANSITION", item
    assert item["error"]["errors"][0]["field"] == "facts.category", item
    assert item["job_id"] is None
    assert len(pub.fake.calls) == calls


async def test_retry_fields_after_recategorize_resends_nothing(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    made, product, _ = await _rejected(api, pub)
    assert (await _post(api, pub, product)).status_code == 200
    calls = len(pub.fake.calls)
    response = await retry(api, pub, made)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["type"] == "INVALID_STATUS_TRANSITION"
    assert len(pub.fake.calls) == calls
