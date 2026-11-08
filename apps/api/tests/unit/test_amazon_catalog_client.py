"""SI13 T3 (P:A3; AC6): the SP-API client's two read-only pre-check calls, searchCatalogItems and
getListingsRestrictions, over the in-process fake. Every refusal happens before the limiter or a
token is touched; the request carries only barcodes, the ASIN, the seller id and the marketplace."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from tests.adapters.amazon_support import SELLER, FixedRng, spapi_client
from tests.support.fake_marketplace import build_fake

EAN = "8901234567890"


async def _control(app: Any, **body: Any) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://fake.test"
    ) as client:
        await client.post("/__control", json=body)


def _calls(app: Any) -> list[dict[str, Any]]:
    return list(app.state.fake.calls)


# ---- happy paths: byte-exact URLs and the limiter bucket ----


async def test_search_catalog_url_and_bucket() -> None:
    app = build_fake()
    client = spapi_client(app)
    response = await client.search_catalog([EAN, "8900000000017"], "EAN")
    assert response.status == 200 and response.json == {"numberOfResults": 0, "items": []}
    (call,) = _calls(app)
    assert f"{call['raw_path']}?{call['query']}" == (
        "/catalog/2022-04-01/items?identifiers=8901234567890%2C8900000000017"
        "&identifiersType=EAN&marketplaceIds=A21TJRUUN4KGV&includedData=summaries%2Cidentifiers"
        "&pageSize=20"
    )
    assert client._limiter.calls == ["catalog_search"]


async def test_get_restrictions_url_and_bucket() -> None:
    app = build_fake()
    client = spapi_client(app)
    response = await client.get_restrictions("B0ABCDEF12")
    assert response.json == {"restrictions": []}
    (call,) = _calls(app)
    assert f"{call['raw_path']}?{call['query']}" == (
        f"/listings/2021-08-01/restrictions?asin=B0ABCDEF12&sellerId={SELLER}"
        "&marketplaceIds=A21TJRUUN4KGV&conditionType=new_new"
    )
    assert client._limiter.calls == ["restrictions_get"]


async def test_only_ids_are_sent() -> None:
    app = build_fake()
    client = spapi_client(app)
    await client.search_catalog([EAN], "EAN")
    await client.get_restrictions("B0ABCDEF12")
    for call in _calls(app):
        assert call["method"] == "GET"
        assert call["body_sha256"] == (
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"  # empty body
        )
        names = {pair.split("=")[0] for pair in call["query"].split("&")}
        assert names <= {
            "identifiers",
            "identifiersType",
            "marketplaceIds",
            "includedData",
            "pageSize",
            "pageToken",
            "asin",
            "sellerId",
            "conditionType",
        }


# ---- refusals: before the limiter and the token ----


@pytest.mark.parametrize(
    "identifiers",
    [
        [f"89000000000{i:02d}" for i in range(21)],
        [],
        [""],
        ["89012345678a"],
        [
            "\uff18\uff19\uff10\uff11\uff12\uff13\uff14\uff15\uff16\uff17\uff18\uff19\uff10"
        ],  # full-width digits
        ["8901-234567890"],
        ["1234567"],
        ["123456789012345"],
        EAN,  # a bare string is not a list of barcodes
    ],
)
async def test_search_refusals(identifiers: Any) -> None:
    app = build_fake()
    client = spapi_client(app)
    with pytest.raises(ValueError):
        await client.search_catalog(identifiers, "EAN")
    assert client._limiter.calls == [] and _calls(app) == []


async def test_search_refuses_an_unknown_type() -> None:
    client = spapi_client(build_fake())
    with pytest.raises(ValueError):
        await client.search_catalog([EAN], "ISBN")
    assert client._limiter.calls == []


@pytest.mark.parametrize(
    "asin", ["B0ABC/EF12", "B0ABCDEF1", "b0abcdef12", "B0ABCDEF12 ", "", "../x"]
)
async def test_restriction_refusals(asin: str) -> None:
    app = build_fake()
    client = spapi_client(app)
    with pytest.raises(ValueError):
        await client.get_restrictions(asin)
    assert client._limiter.calls == [] and _calls(app) == []


async def test_restrictions_need_a_seller() -> None:
    app = build_fake()
    client = spapi_client(app, seller_id=None)
    with pytest.raises(ValueError, match="seller_id"):
        await client.get_restrictions("B0ABCDEF12")
    assert client._limiter.calls == []


# ---- errors follow `_call` ----


async def test_catalog_5xx_is_unavailable() -> None:
    from app.adapters.base import MarketplaceUnavailable

    app = build_fake()
    await _control(app, spapi_catalog={EAN: {"scenario": "catalog_5xx"}})
    with pytest.raises(MarketplaceUnavailable):
        await spapi_client(app).search_catalog([EAN], "EAN")


async def test_catalog_429_backs_off_then_answers() -> None:
    app = build_fake()
    client = spapi_client(app, rng=FixedRng(0.25))
    app.state.fake.spapi_429 = 0
    calls = {"n": 0}
    inner = httpx.ASGITransport(app)

    class Throttle(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, json={"errors": [{"code": "QuotaExceeded"}]})
            return await inner.handle_async_request(request)

    client = spapi_client(app, transport=Throttle(), rng=FixedRng(0.25))
    response = await client.search_catalog([EAN], "EAN")
    assert response.status == 200
    assert client.sleeps == [1.25]
    assert client._limiter.calls == ["catalog_search", "catalog_search"]


async def test_the_limiter_refuses_an_unknown_operation() -> None:
    from app.adapters.amazon import constants as c
    from app.workers.outbound_limiter import PgOutboundLimiter

    limiter = PgOutboundLimiter("amazon", "00000000-0000-0000-0000-000000000000", rates=c.RATES)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown marketplace operation"):
        await limiter.acquire("catalog_delete")
    assert {"catalog_search", "restrictions_get"} <= set(c.RATES)
    assert tuple(c.RATES) == c.OPERATIONS


# ---- round 1 fix: pages (pageSize default 10, max 20; nextToken -> pageToken) ----


async def test_search_follows_next_token_and_merges_pages() -> None:
    app = build_fake()
    eans = [f"89000000{n:05d}" for n in range(15)]
    await _control(
        app, spapi_catalog={e: {"scenario": "other_brand_ean", "copies": "2"} for e in eans}
    )
    client = spapi_client(app)
    response = await client.search_catalog(eans, "EAN")
    assert len(response.json["items"]) == 30 and "pagination" not in response.json
    calls = _calls(app)
    assert len(calls) == 2 and "pageToken=o20" in calls[1]["query"]
    assert client._limiter.calls == ["catalog_search", "catalog_search"]


async def test_search_stops_at_the_page_bound_and_keeps_the_token() -> None:
    app = build_fake()
    eans = [f"89000000{n:05d}" for n in range(20)]
    await _control(
        app, spapi_catalog={e: {"scenario": "other_brand_ean", "copies": "6"} for e in eans}
    )
    response = await spapi_client(app).search_catalog(eans, "EAN")
    assert len(_calls(app)) == 5 and len(response.json["items"]) == 100
    assert response.json["pagination"] == {"nextToken": "o100"}


async def test_a_failed_later_page_keeps_the_token() -> None:
    app = build_fake()
    eans = [f"89000000{n:05d}" for n in range(15)]
    await _control(
        app,
        spapi_catalog={e: {"scenario": "other_brand_ean", "copies": "2"} for e in eans},
        spapi_catalog_page_5xx=True,
    )
    response = await spapi_client(app).search_catalog(eans, "EAN")
    assert len(response.json["items"]) == 20
    assert response.json["pagination"] == {"nextToken": "o20"}
