"""SI13 AC3 attack (tester): the recategorise of a rejected listing keeps `failed` and its old
approval until the seller regenerates (DEVIATIONS T8). Neither publish path may resend that
pre-recategorise version: both are refused as a duplicate submission."""

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
    assert item["error"]["error_code"] == "DUPLICATE_SUBMISSION_BLOCKED", item
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
    assert response.json()["error"]["type"] == "DUPLICATE_SUBMISSION_BLOCKED"
    assert len(pub.fake.calls) == calls
