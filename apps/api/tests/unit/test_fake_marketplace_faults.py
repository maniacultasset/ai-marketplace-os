"""S26 T9 (AC 2 API10, AC 5): the fake marketplace's failure-injection switches, over ASGI."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from tests.support.fake_marketplace import OVERSIZE_BYTES, build_fake, issue_token
from tests.unit.test_fake_marketplace import _refresh_token, _token
from tests.unit.test_fake_marketplace import fake as oauth_fake

BASE = "/listings/2021-08-01/items/A1SELLER"
Q = {"marketplaceIds": "A21TJRUUN4KGV"}
CANARY = "CANARYzq9-s26fault0000000a"
__all__ = ["oauth_fake"]


@pytest.fixture
async def spapi() -> AsyncIterator[tuple[httpx.AsyncClient, str, Any]]:
    app = build_fake()
    token = issue_token(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://fake.test"
    ) as client:
        yield client, token, app


def _body() -> dict[str, Any]:
    return {"productType": "KURTA", "attributes": {"item_name": [{"value": "Kurta"}]}}


async def test_fake_spapi_malformed_json(spapi: Any) -> None:
    client, token, _app = spapi
    await client.post("/__control", json={"spapi_body": "malformed_json"})
    first = await client.put(
        f"{BASE}/SKU-1", params=Q, json=_body(), headers={"x-amz-access-token": token}
    )
    assert first.status_code == 200
    with pytest.raises(json.JSONDecodeError):
        first.json()
    again = await client.put(
        f"{BASE}/SKU-1", params=Q, json=_body(), headers={"x-amz-access-token": token}
    )
    assert again.json()["status"] == "ACCEPTED"  # once only


async def test_fake_spapi_extra_field(spapi: Any) -> None:
    client, token, app = spapi
    await client.post("/__control", json={"spapi_body": "extra_field"})
    reply = await client.put(
        f"{BASE}/SKU-1", params=Q, json=_body(), headers={"x-amz-access-token": token}
    )
    body = reply.json()
    assert body["unexpected"] == 1 and body["status"] == "ACCEPTED" and body["sku"] == "SKU-1"
    assert ("A1SELLER", "SKU-1") in app.state.fake.spapi_items


async def test_fake_spapi_drop_after_write_stores_then_drops(spapi: Any) -> None:
    client, token, app = spapi
    await client.post("/__control", json={"spapi_drop_after_write": ["SKU-1"]})
    with pytest.raises(httpx.RemoteProtocolError):
        await client.put(
            f"{BASE}/SKU-1", params=Q, json=_body(), headers={"x-amz-access-token": token}
        )
    assert ("A1SELLER", "SKU-1") in app.state.fake.spapi_items  # the write landed
    assert len(app.state.fake.calls) == 1
    got = await client.get(f"{BASE}/SKU-1", params=Q, headers={"x-amz-access-token": token})
    assert got.status_code == 200  # a read-before-retry sees it


@pytest.mark.parametrize("outcome", ["malformed", "oversize", "extra_field"])
async def test_fake_refresh_fault_replies_carry_canary(
    oauth_fake: httpx.AsyncClient, outcome: str
) -> None:
    refresh = await _refresh_token(oauth_fake)
    await oauth_fake.post("/__control", json={"next_refresh": outcome, "canary": CANARY})
    reply = await _token(oauth_fake, "amazon", grant_type="refresh_token", refresh_token=refresh)
    assert reply.status_code == 200
    assert CANARY in reply.text
    if outcome == "malformed":
        with pytest.raises(json.JSONDecodeError):
            reply.json()
    elif outcome == "oversize":
        assert len(reply.content) > OVERSIZE_BYTES
    else:
        assert reply.json()["unexpected"] == CANARY and reply.json()["access_token"]
    after = await _token(oauth_fake, "amazon", grant_type="refresh_token", refresh_token=refresh)
    assert after.status_code == 200 and "unexpected" not in after.json()  # once only
