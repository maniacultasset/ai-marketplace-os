"""S20 T10 (AC3 backoff, AC11, AC12; C2, C4, C16): the SP-API HTTP client, over the in-process
fake (no network)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from tests.adapters.amazon_support import SELLER, FakeTokens, FixedRng, spapi_client
from tests.support.fake_marketplace import CANARY, build_fake
from tests.support.fault_transport import FaultTransport

BODY = {"productType": "KURTA", "requirements": "LISTING_PRODUCT_ONLY", "attributes": {"item_name": [{"value": "K"}]}}  # fmt: skip


async def _control(app: Any, **body: Any) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://fake.test"
    ) as client:
        await client.post("/__control", json=body)


def _calls(app: Any) -> list[dict[str, Any]]:
    return list(app.state.fake.calls)


async def test_amazon_throttling() -> None:
    app = build_fake()
    await _control(app, spapi_429=2)
    client = spapi_client(app, rng=FixedRng(0.25))
    response = await client.put("SKU-1", BODY)
    assert response.status == 200 and response.json["status"] == "ACCEPTED"
    assert client.sleeps == [1.25, 2.25]
    assert len(_calls(app)) == 3


async def test_amazon_429_budget_exhausted_rate_limited() -> None:
    from app.adapters.base import RateLimited
    from mkos_schemas.codes import ErrorCode

    app = build_fake()
    await _control(app, spapi_429=10)
    client = spapi_client(app)
    with pytest.raises(RateLimited) as caught:
        await client.put("SKU-1", BODY)
    assert caught.value.report.error_code is ErrorCode.RATE_LIMITED
    assert caught.value.completed == ()
    assert len(_calls(app)) == 4 and len(client.sleeps) == 3
    assert app.state.fake.spapi_items == {}


def test_amazon_backoff_cap_60s() -> None:
    from app.adapters.amazon.client import backoff_delay

    assert backoff_delay(0, FixedRng(0.0)) == 1.0
    assert backoff_delay(5, FixedRng(0.5)) == 32.5
    assert backoff_delay(6, FixedRng(0.5)) == 60.0
    assert backoff_delay(20, FixedRng(0.0)) == 60.0


async def test_amazon_limiter_before_every_try() -> None:
    app = build_fake()
    await _control(app, spapi_429=1)
    client = spapi_client(app)
    await client.get("SKU-1")
    assert client._limiter.calls == ["get", "get"]


async def test_amazon_transport_connect_error_unavailable() -> None:
    from app.adapters.base import MarketplaceUnavailable

    app = build_fake()
    transport = FaultTransport(httpx.ASGITransport(app), before_send=httpx.ConnectError("x"))
    with pytest.raises(MarketplaceUnavailable) as caught:
        await spapi_client(app, transport=transport).put("SKU-1", BODY)
    assert caught.value.report.error_code == "MARKETPLACE_UNAVAILABLE"
    assert _calls(app) == []


async def test_amazon_transport_5xx_after_write_ambiguous() -> None:
    from app.adapters.base import MarketplaceAmbiguous

    app = build_fake()
    await _control(app, spapi_after_write={"SKU-1": 500})
    with pytest.raises(MarketplaceAmbiguous) as caught:
        await spapi_client(app).put("SKU-1", BODY)
    assert caught.value.report.error_code == "MARKETPLACE_UNCONFIRMED"
    assert (SELLER, "SKU-1") in app.state.fake.spapi_items


async def test_amazon_transport_timeout_after_write_ambiguous() -> None:
    from app.adapters.base import MarketplaceAmbiguous

    app = build_fake()
    transport = FaultTransport(httpx.ASGITransport(app), after_write=httpx.ReadTimeout("t"))
    with pytest.raises(MarketplaceAmbiguous):
        await spapi_client(app, transport=transport).patch("SKU-1", {"patches": []})


async def test_amazon_transport_get_failure_unavailable() -> None:
    from app.adapters.base import MarketplaceUnavailable

    app = build_fake()
    await _control(app, spapi_body="html")
    with pytest.raises(MarketplaceUnavailable):
        await spapi_client(app).get("SKU-1")


async def test_amazon_transport_redirect_not_followed() -> None:
    from app.adapters.base import MarketplaceAmbiguous

    app = build_fake()
    await _control(app, spapi_body="redirect")
    with pytest.raises(MarketplaceAmbiguous):
        await spapi_client(app).put("SKU-1", BODY)
    assert len(_calls(app)) == 1  # the redirect target was never asked


async def test_amazon_transport_oversize_body() -> None:
    from app.adapters.base import MarketplaceAmbiguous

    app = build_fake()
    await _control(app, spapi_body="oversize")
    with pytest.raises(MarketplaceAmbiguous) as caught:
        await spapi_client(app).put("SKU-1", BODY)
    assert "oversized" in caught.value.report.errors[0].message


async def test_amazon_transport_non_json() -> None:
    from app.adapters.base import MarketplaceAmbiguous

    app = build_fake()
    await _control(app, spapi_body="html")
    with pytest.raises(MarketplaceAmbiguous):
        await spapi_client(app).put("SKU-1", BODY)


@pytest.mark.parametrize(
    ("sku", "segment"),
    [
        ("a/b", "a%2Fb"),
        ("कुर्ता-1", "%E0%A4%95%E0%A5%81%E0%A4%B0%E0%A5%8D%E0%A4%A4%E0%A4%BE-1"),
        ("50%off", "50%25off"),
        ("a.b", "a.b"),
        ("a?b#c", "a%3Fb%23c"),
    ],
    ids=["slash", "unicode", "percent", "a.b", "query-chars"],
)
async def test_amazon_transport_raw_path_matches_template(sku: str, segment: str) -> None:
    app = build_fake()
    client = spapi_client(app)
    await client.put(sku, BODY)
    await client.get(sku)
    put, get = _calls(app)
    assert put["raw_path"] == f"/listings/2021-08-01/items/{SELLER}/{segment}"
    assert get["raw_path"] == put["raw_path"]
    assert (SELLER, sku) in app.state.fake.spapi_items


@pytest.mark.parametrize("sku", [".", ".."])
async def test_amazon_transport_sku_dot_segment_rejected(sku: str) -> None:
    app = build_fake()
    client = spapi_client(app)
    with pytest.raises(ValueError, match="refused"):
        await client.put(sku, BODY)
    with pytest.raises(ValueError, match="refused"):
        await client.patch(sku, BODY)
    with pytest.raises(ValueError, match="refused"):
        await client.get(sku)
    assert _calls(app) == []
    assert client._limiter.calls == []


async def test_amazon_auth_refresh_once_in_client() -> None:
    from app.adapters.base import AuthExpired

    app = build_fake()
    tokens = FakeTokens(app)
    client = spapi_client(app, tokens=tokens)
    await _control(app, spapi_auth={"mode": "expired", "times": 1})
    assert (await client.get("SKU-1")).status == 404
    assert tokens.refreshes == 1
    await _control(app, spapi_auth={"mode": "expired", "times": 1})
    with pytest.raises(AuthExpired):
        await client.get("SKU-1")
    assert tokens.refreshes == 1


async def test_amazon_call_logging(log_lines: Any) -> None:
    app = build_fake()
    capture = log_lines()
    await _control(app, spapi_issues={"SKU-1": [{"code": "90220", "message": "m", "severity": "ERROR", "attributeNames": ["item_name"]}]})  # fmt: skip
    await spapi_client(app).put("SKU-1", BODY)
    lines = capture.lines()
    (line,) = [one for one in lines if one.get("message") == "marketplace.call"]
    keys = ("operation", "method", "path", "status", "attempt", "issue_code", "marketplace")
    assert {k: line[k] for k in keys} == {
        "operation": "put",
        "method": "PUT",
        "path": "/listings/2021-08-01/items/{sellerId}/{sku}",
        "status": 200,
        "attempt": 1,
        "issue_code": "90220",
        "marketplace": "amazon",
    }
    assert isinstance(line["duration_ms"], float) and "rate_limit" in line
    text = capture.text()
    assert "SKU-1" not in text and SELLER not in text and "fake.test" not in text
    assert len(lines) == 1  # httpx says nothing below WARNING


async def test_amazon_tokens_never_leak(log_lines: Any, sentry_events: Any) -> None:
    from app.adapters.base import PermissionDenied
    from app.core.config import Settings
    from app.core.observability import init_sentry

    init_sentry(Settings(_env_file=None, app_env="local", app_role="shell", sentry_dsn="https://k@o0.ingest.sentry.io/1"))  # fmt: skip
    app = build_fake()
    tokens = FakeTokens(app)
    capture = log_lines()
    await _control(app, spapi_auth={"mode": "denied", "times": 5})
    with pytest.raises(PermissionDenied) as caught:
        await spapi_client(app, tokens=tokens).put("SKU-1", BODY)
    import sentry_sdk

    sentry_sdk.capture_exception(caught.value)
    sentry_sdk.get_client().flush(timeout=2)
    assert tokens.token.startswith(CANARY)
    for text in (
        capture.text(),
        sentry_events.serialized(),
        str(caught.value),
        repr(caught.value),
        caught.value.report.model_dump_json(),
    ):
        assert CANARY not in text
