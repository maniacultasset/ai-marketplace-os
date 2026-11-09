"""The Amazon connection check's safety net and its new read (`app.workers.amazon_check`):
`GetOnlyTransport` refuses every method but GET before anything is sent, the SP-API client's
write routes cannot pass through it, and getMarketplaceParticipations is a bare GET."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from app.adapters.amazon.client import SpApiClient
from app.workers.amazon_check import (
    CheckReport,
    CheckRow,
    GetOnlyTransport,
    WriteRefused,
    mask_seller,
)
from tests.adapters.amazon_support import SELLER, FixedRng, spapi_client
from tests.support.fake_marketplace import PARTICIPATIONS_RATE, build_fake


class Recorder(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.seen: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request.method)
        return httpx.Response(200, json={}, headers={"x-amzn-RateLimit-Limit": "5.0"})


async def _control(app: Any, **body: Any) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://fake.test"
    ) as client:
        await client.post("/__control", json=body)


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE", "POST", "HEAD", "OPTIONS"])
async def test_transport_refuses_every_method_but_get(method: str) -> None:
    inner = Recorder()
    guard = GetOnlyTransport(inner)
    async with httpx.AsyncClient(transport=guard, base_url="http://fake.test") as client:
        with pytest.raises(WriteRefused):
            await client.request(method, "/listings/2021-08-01/items/A1/SKU", content=b"{}")
        reply = await client.get("/sellers/v1/marketplaceParticipations")
    assert reply.status_code == 200
    assert inner.seen == ["GET"]  # the refused request never reached the network
    assert guard.methods == {"GET": 1} and guard.refused == {method: 1}
    assert guard.rate_limits == {"sellers": "5.0"}


async def test_transport_keeps_only_a_numeric_rate_header() -> None:
    class Odd(Recorder):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={}, headers={"x-amzn-RateLimit-Limit": "1; secret"})

    guard = GetOnlyTransport(Odd())
    async with httpx.AsyncClient(transport=guard, base_url="http://fake.test") as client:
        await client.get("/catalog/2022-04-01/items")
    assert guard.rate_limits == {}


async def test_client_write_routes_cannot_pass_the_guard() -> None:
    """put, patch and the VALIDATION_PREVIEW dry run all raise before the fake sees them."""
    app = build_fake()
    client: SpApiClient = spapi_client(app, transport=GetOnlyTransport(httpx.ASGITransport(app)))
    body = {"productType": "KURTA", "requirements": "LISTING", "attributes": {}}
    with pytest.raises(WriteRefused):
        await client.put("SKU-1", body)
    with pytest.raises(WriteRefused):
        await client.patch("SKU-1", {"productType": "KURTA", "patches": []})
    with pytest.raises(WriteRefused):
        await client.put_preview("SKU-1", body)
    assert app.state.fake.calls == []
    assert app.state.fake.spapi_items == {} and app.state.fake.spapi_preview_calls == 0
    got = await client.get("SKU-1")
    assert got.status == 404
    assert [c["method"] for c in app.state.fake.calls] == ["GET"]


async def test_participations_is_a_bare_get() -> None:
    app = build_fake()
    client: SpApiClient = spapi_client(app, rng=FixedRng(0.0))
    reply = await client.get_participations()
    assert reply.status == 200
    (call,) = app.state.fake.calls
    assert (call["method"], call["raw_path"], call["query"]) == (
        "GET",
        "/sellers/v1/marketplaceParticipations",
        "",
    )
    (entry,) = reply.json["payload"]
    assert entry["marketplace"]["id"] == "A21TJRUUN4KGV"
    assert entry["participation"] == {"isParticipating": True, "hasSuspendedListings": False}
    assert reply.headers["x-amzn-ratelimit-limit"] == PARTICIPATIONS_RATE
    assert SELLER not in call["raw_path"]


@pytest.mark.parametrize(
    ("scenario", "marketplace", "participating", "suspended"),
    [
        ("suspended", "A21TJRUUN4KGV", True, True),
        ("not_participating", "A21TJRUUN4KGV", False, False),
        ("other_only", "A2VIGQ35RCS4UG", True, False),
    ],
)
async def test_fake_participation_scenarios(
    scenario: str, marketplace: str, participating: bool, suspended: bool
) -> None:
    app = build_fake()
    await _control(app, spapi_participations=scenario)
    reply = await spapi_client(app).get_participations()
    (entry,) = reply.json["payload"]
    assert entry["marketplace"]["id"] == marketplace
    assert entry["participation"] == {
        "isParticipating": participating,
        "hasSuspendedListings": suspended,
    }


async def test_participations_refuses_an_unknown_token() -> None:
    app = build_fake()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://fake.test"
    ) as client:
        reply = await client.get(
            "/sellers/v1/marketplaceParticipations", headers={"x-amz-access-token": "nope"}
        )
    assert reply.status_code == 403


def test_mask_seller_never_shows_the_whole_id() -> None:
    assert mask_seller("A1B2C3D4E5F6G7") == "A1B...6G7"
    assert mask_seller("A1B2C3") == "******"


def test_report_result_and_table() -> None:
    tenant = uuid.uuid4()
    ok = CheckRow("1 account", "PASS", "ok", 3.2)
    warn = CheckRow("4 ptd SHIRT", "WARN", "stale", 10.0)
    fail = CheckRow("5 listings", "FAIL", "403", 1.0)
    assert CheckReport(tenant, rows=[ok]).result == "PASS"
    assert CheckReport(tenant, rows=[ok, warn]).passed
    assert CheckReport(tenant, rows=[ok, warn]).result == "WARN"
    report = CheckReport(tenant, rows=[ok, warn, fail], gets=4)
    assert report.result == "FAIL" and not report.passed
    text = report.render()
    assert text.splitlines()[1].split() == ["STEP", "RESULT", "TIME", "DETAIL"]
    assert "4 ptd SHIRT  WARN" in text
    assert text.splitlines()[-1].startswith("amazon check: FAIL (1 pass, 1 warn, 1 fail, 0 skip)")
    assert CheckReport(tenant, refused=1).result == "FAIL"
