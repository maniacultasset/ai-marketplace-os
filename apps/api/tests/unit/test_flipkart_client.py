"""S22 T10 (AC6, AC10, AC11; S20 C2-C4; C8): the Flipkart listings v3 client, over the
in-process fake (no network)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from tests.adapters.flipkart_support import FakeTokens, FixedRng, flipkart_client
from tests.support.fake_marketplace import CANARY, build_fake
from tests.support.fault_transport import FaultTransport

LISTING = {"sku": "SKU-1", "vertical": "kurta", "attributes": {"title": [{"value": "K"}]}}


async def _control(app: Any, **body: Any) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://fake.test"
    ) as client:
        await client.post("/__control", json=body)


def _calls(app: Any) -> list[dict[str, Any]]:
    return [c for c in app.state.fake.calls if c["marketplace"] == "flipkart"]


async def test_flipkart_throttling() -> None:
    app = build_fake()
    await _control(app, fk_429=2)
    client = flipkart_client(app, rng=FixedRng(0.25))
    response = await client.create([LISTING])
    assert response.status == 200 and response.json["results"][0]["status"] == "SUCCESS"
    assert response.request_id is not None and response.request_id.startswith("fk-req-")
    assert client.sleeps == [1.25, 2.25]
    assert len(_calls(app)) == 3
    assert client._limiter.calls == ["create"] * 3


async def test_flipkart_429_budget_exhausted_rate_limited() -> None:
    from app.adapters.base import RateLimited
    from mkos_schemas.codes import ErrorCode

    app = build_fake()
    await _control(app, fk_429=10)
    client = flipkart_client(app)
    with pytest.raises(RateLimited) as caught:
        await client.create([LISTING])
    report = caught.value.report
    assert report.error_code is ErrorCode.RATE_LIMITED
    assert report.errors[0].source.value == "flipkart_adapter"
    assert caught.value.completed == ()
    assert len(_calls(app)) == 4 and len(client.sleeps) == 3
    assert app.state.fake.fk_items == {}


async def test_flipkart_transport_connect_error_unavailable() -> None:
    from app.adapters.base import MarketplaceUnavailable

    app = build_fake()
    fault = FaultTransport(httpx.ASGITransport(app), before_send=httpx.ConnectError("x"))
    with pytest.raises(MarketplaceUnavailable) as caught:
        await flipkart_client(app, transport=fault).create([LISTING])
    assert caught.value.report.error_code == "MARKETPLACE_UNAVAILABLE"
    assert app.state.fake.fk_items == {}


async def test_flipkart_transport_5xx_after_write_ambiguous() -> None:
    from app.adapters.base import MarketplaceAmbiguous

    app = build_fake()
    await _control(app, fk_after_write=503)
    with pytest.raises(MarketplaceAmbiguous) as caught:
        await flipkart_client(app).create([LISTING])
    assert caught.value.report.error_code == "MARKETPLACE_UNCONFIRMED"
    assert len(app.state.fake.fk_items) == 1  # applied, then the reply failed


async def test_flipkart_transport_timeout_after_write_ambiguous() -> None:
    from app.adapters.base import MarketplaceAmbiguous

    app = build_fake()
    fault = FaultTransport(httpx.ASGITransport(app), after_write=httpx.ReadTimeout("x"))
    with pytest.raises(MarketplaceAmbiguous):
        await flipkart_client(app, transport=fault).create([LISTING])
    assert len(app.state.fake.fk_items) == 1


async def test_flipkart_transport_get_failure_unavailable() -> None:
    from app.adapters.base import MarketplaceUnavailable

    app = build_fake()
    await _control(app, fk_body="html")
    with pytest.raises(MarketplaceUnavailable):
        await flipkart_client(app).get(["SKU-1"])


async def test_flipkart_transport_redirect_not_followed() -> None:
    from app.adapters.base import MarketplaceAmbiguous

    app = build_fake()
    await _control(app, fk_body="redirect")
    with pytest.raises(MarketplaceAmbiguous):
        await flipkart_client(app).create([LISTING])
    assert len(_calls(app)) == 1


async def test_flipkart_transport_oversize_body() -> None:
    from app.adapters.base import MarketplaceAmbiguous

    app = build_fake()
    await _control(app, fk_body="oversize")
    with pytest.raises(MarketplaceAmbiguous) as caught:
        await flipkart_client(app).create([LISTING])
    assert "oversized" in caught.value.report.errors[0].message


async def test_flipkart_transport_non_json() -> None:
    from app.adapters.base import MarketplaceAmbiguous

    app = build_fake()
    await _control(app, fk_body="html")
    with pytest.raises(MarketplaceAmbiguous):
        await flipkart_client(app).create([LISTING])


@pytest.mark.parametrize(
    ("sku", "segment"),
    [
        ("कुर्ता-1", "%E0%A4%95%E0%A5%81%E0%A4%B0%E0%A5%8D%E0%A4%A4%E0%A4%BE-1"),
        ("50%off", "50%25off"),
        ("a.b", "a.b"),
        ("a?b#c", "a%3Fb%23c"),
    ],
    ids=["unicode", "percent", "a.b", "query-chars"],
)
async def test_flipkart_unicode_sku_encoded(sku: str, segment: str) -> None:
    app = build_fake()
    client = flipkart_client(app)
    await client.create([{**LISTING, "sku": sku}])
    got = await client.get([sku, "OTHER"])
    post, get = _calls(app)
    assert post["raw_path"] == "/flipkart/sellers/listings/v3"
    assert get["raw_path"] == f"/flipkart/sellers/listings/v3/{segment},OTHER"
    assert [i["sku"] for i in got.json["listings"]] == [sku]


@pytest.mark.parametrize("sku", ["A,B", "A/B", ".", ".."], ids=["comma", "slash", "dot", "dotdot"])
async def test_flipkart_sku_reserved_chars_client(sku: str) -> None:
    app = build_fake()
    client = flipkart_client(app)
    for call in (
        client.create([{**LISTING, "sku": sku}]),
        client.update([{**LISTING, "sku": sku}]),
        client.get([sku]),
    ):
        with pytest.raises(ValueError, match="refused"):
            await call
    assert _calls(app) == [] and client._limiter.calls == []


async def test_flipkart_auth_refresh_once_in_client() -> None:
    from app.adapters.base import AuthExpired

    app = build_fake()
    tokens = FakeTokens(app)
    client = flipkart_client(app, tokens=tokens)
    await _control(app, fk_auth={"mode": "expired", "times": 1})
    assert (await client.get(["SKU-1"])).json == {"listings": []}
    assert tokens.refreshes == 1
    await _control(app, fk_auth={"mode": "expired", "times": 1})
    with pytest.raises(AuthExpired):
        await client.get(["SKU-1"])
    assert tokens.refreshes == 1


async def test_flipkart_call_logging(log_lines: Any) -> None:
    app = build_fake()
    capture = log_lines()
    await _control(app, fk_errors={"SKU-1": {"title": "FK-90"}})
    await flipkart_client(app).create([LISTING])
    lines = capture.lines()
    (line,) = [one for one in lines if one.get("message") == "marketplace.call"]
    keys = ("operation", "method", "path", "status", "attempt", "error_code", "marketplace")
    assert {k: line[k] for k in keys} == {
        "operation": "create",
        "method": "POST",
        "path": "/listings/v3",
        "status": 200,
        "attempt": 1,
        "error_code": "FK-90",
        "marketplace": "flipkart",
    }
    assert isinstance(line["duration_ms"], float)
    text = capture.text()
    assert "SKU-1" not in text and "127.0.0.1" not in text and "Bearer" not in text
    assert CANARY not in text and '"K"' not in text
    assert len(lines) == 1
