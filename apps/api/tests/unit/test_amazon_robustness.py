"""Audit JOBS-1: a deeply nested JSON reply (well under the body caps) from SP-API or from an
OAuth token endpoint is a protocol error, never a raw RecursionError. Ported from
test_flipkart_robustness.py::test_flipkart_deeply_nested_json_is_protocol_error."""

from __future__ import annotations

import httpx
import pytest
from app.adapters.base import MarketplaceAmbiguous, MarketplaceUnavailable
from app.adapters.oauth.base import OAuthProtocolError, post_token
from tests.adapters.amazon_support import spapi_client
from tests.support.fake_marketplace import build_fake

BODY = {"productType": "KURTA", "requirements": "LISTING_PRODUCT_ONLY", "attributes": {}}


def _nested(depth: int) -> httpx.MockTransport:
    body = b"[" * depth + b"]" * depth
    return httpx.MockTransport(lambda request: httpx.Response(200, content=body))


@pytest.mark.parametrize("depth", [100_000])  # 200 KB: far under the 1 MiB SP-API cap
async def test_amazon_deeply_nested_json_is_protocol_error(depth: int) -> None:
    app = build_fake()
    client = spapi_client(app, transport=_nested(depth))
    with pytest.raises(MarketplaceAmbiguous):
        await client.put("SKU-1", BODY)
    client = spapi_client(app, transport=_nested(depth))
    with pytest.raises(MarketplaceAmbiguous):
        await client.patch("SKU-1", {"productType": "KURTA", "patches": []})
    client = spapi_client(app, transport=_nested(depth))
    with pytest.raises(MarketplaceUnavailable):
        await client.get("SKU-1")


@pytest.mark.parametrize("depth", [30_000])  # 60 KB: under the 64 KiB token-reply cap
async def test_oauth_token_reply_deeply_nested_json_is_protocol_error(
    depth: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.adapters.oauth.http.make_transport", lambda: _nested(depth))
    with pytest.raises(OAuthProtocolError):
        await post_token("https://api.amazon.com/auth/o2/token", {"grant_type": "refresh_token"})
