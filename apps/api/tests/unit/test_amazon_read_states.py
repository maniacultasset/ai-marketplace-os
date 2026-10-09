"""S21 T18 (CP6, CP15; AC10): the states `AmazonAdapter.get_listing` reads off the fake SP-API's
GET, and the fake's per-SKU GET control and prefix reset."""

from __future__ import annotations

import httpx
import pytest
from tests.adapters.amazon_support import amazon_adapter
from tests.support.fake_marketplace import build_fake, reset_prefix

from .test_amazon_adapter import _five


@pytest.mark.parametrize(
    ("mode", "state", "status"),
    [
        ("buyable", "live", "published"),
        ("discoverable", "processing", "published"),
        ("inactive", "inactive", "published"),
        ("rejected", "rejected", "failed"),
    ],
)
async def test_amazon_get_states(mode: str, state: str, status: str) -> None:
    app = build_fake()
    adapter = amazon_adapter(app)
    sent = _five()
    await adapter.create_listing(sent)
    app.state.fake.spapi_get[sent.seller_sku] = mode
    got = await adapter.get_listing(sent.seller_sku, listing_id=sent.listing_id)
    assert (got.marketplace_state, got.status) == (state, status)


async def test_amazon_get_gone_raises_not_found() -> None:
    from app.adapters.base import ListingNotFound

    app = build_fake()
    adapter = amazon_adapter(app)
    sent = _five()
    await adapter.create_listing(sent)
    app.state.fake.spapi_get[sent.seller_sku] = "gone"
    with pytest.raises(ListingNotFound):
        await adapter.get_listing(sent.seller_sku)


async def test_fake_get_control_and_reset_prefix() -> None:
    app = build_fake()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://fake") as client:
        response = await client.post(
            "/__control", json={"spapi_get": {"E2E-A": "buyable", "X": "bad"}}
        )
        assert response.status_code == 200
        assert app.state.fake.spapi_get == {"E2E-A": "buyable"}
        app.state.fake.spapi_items[("S", "E2E-A")] = {
            "productType": "x",
            "attributes": {},
            "issues": [],
        }
        app.state.fake.spapi_items[("S", "OTHER")] = {
            "productType": "x",
            "attributes": {},
            "issues": [],
        }
        response = await client.post("/__control/reset", json={"prefix": "E2E-"})
        assert response.json() == {"reset": 1}
    assert list(app.state.fake.spapi_items) == [("S", "OTHER")]
    assert app.state.fake.spapi_get == {}
    assert reset_prefix(app.state.fake, "") == 0
