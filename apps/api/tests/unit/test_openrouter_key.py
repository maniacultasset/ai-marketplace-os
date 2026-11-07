"""SI10 T4 (AC3): the OpenRouter `GET /key` adapter, against a fake transport only (no live call,
no real key). The key never reaches a log line or an exception."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from decimal import Decimal

import httpx
import pytest
from app.adapters.openrouter_key import KeyInfo, KeyRefused, KeyUnavailable, fetch_key
from app.core.config import OPENROUTER_BASE_URL

KEY = "sk-or-v1-test-not-a-real-key-0123456789abcdef"  # pragma: allowlist secret
BODY = {
    "data": {
        "label": "kuinn",
        "limit": 10,
        "limit_remaining": 0.4,
        "limit_reset": None,
        "usage": 9.6,
        "usage_daily": 0.2,
        "is_free_tier": False,
    }
}

Handler = Callable[[httpx.Request], httpx.Response]


def _transport(handler: Handler, seen: list[httpx.Request] | None = None) -> httpx.MockTransport:
    def wrapped(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return handler(request)

    return httpx.MockTransport(wrapped)


def _json(body: object, status: int = 200) -> Handler:
    return lambda _r: httpx.Response(status, json=body)


async def test_recorded_body_parses() -> None:
    seen: list[httpx.Request] = []
    info = await fetch_key(KEY, transport=_transport(_json(BODY), seen))
    assert info == KeyInfo(limit=Decimal("10"), remaining=Decimal("0.4"), usage=Decimal("9.6"))
    assert len(seen) == 1
    assert str(seen[0].url) == f"{OPENROUTER_BASE_URL}/key"
    assert seen[0].method == "GET"
    assert seen[0].headers["authorization"] == f"Bearer {KEY}"


async def test_null_limit_reads_unlimited() -> None:
    body = {"data": {**BODY["data"], "limit": None, "limit_remaining": None}}
    info = await fetch_key(KEY, transport=_transport(_json(body)))
    assert info.limit is None
    assert info.remaining is None
    assert info.usage == Decimal("9.6")


@pytest.mark.parametrize("status", [401, 403])
async def test_refused(status: int, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    with pytest.raises(KeyRefused) as caught:
        await fetch_key(KEY, transport=_transport(_json({"error": {"message": KEY}}, status)))
    _no_key(caught.value, caplog)


def _raise(exc: Exception) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return handler


UNAVAILABLE: list[tuple[str, Handler]] = [
    ("429", _json({"error": "slow down"}, 429)),
    ("500", _json({"error": "boom"}, 500)),
    ("503", _json({"error": "down"}, 503)),
    ("timeout", _raise(httpx.ReadTimeout("timed out"))),
    ("connect", _raise(httpx.ConnectError("no route"))),
    (
        "html",
        lambda _r: httpx.Response(
            200, text="<html>oops</html>", headers={"content-type": "text/html"}
        ),
    ),
    ("no data", _json({"data": {}})),
    ("data not an object", _json({"data": [1, 2]})),
    ("top not an object", _json([1, 2])),
    ("nan", _json({"data": {**BODY["data"], "limit_remaining": "NaN"}})),
    ("infinity", _json({"data": {**BODY["data"], "limit_remaining": "Infinity"}})),
    ("negative", _json({"data": {**BODY["data"], "limit_remaining": -1}})),
    ("negative usage", _json({"data": {**BODY["data"], "usage": -0.1}})),
    ("text number", _json({"data": {**BODY["data"], "usage": "nine"}})),
    ("bool", _json({"data": {**BODY["data"], "limit": True}})),
    ("redirect", lambda _r: httpx.Response(302, headers={"location": "https://evil.test/key"})),
]


@pytest.mark.parametrize(("name", "handler"), UNAVAILABLE, ids=[n for n, _ in UNAVAILABLE])
async def test_unavailable(name: str, handler: Handler, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    seen: list[httpx.Request] = []
    with pytest.raises(KeyUnavailable) as caught:
        await fetch_key(KEY, transport=_transport(handler, seen))
    assert len(seen) == 1, "no retry and no redirect follow"
    _no_key(caught.value, caplog)


async def test_nan_in_raw_json_is_unavailable() -> None:
    # json.loads accepts the bare NaN token; Decimal("NaN") would pass a naive parse
    raw = json.dumps(BODY).replace("0.4", "NaN")
    with pytest.raises(KeyUnavailable):
        await fetch_key(
            KEY,
            transport=_transport(
                lambda _r: httpx.Response(
                    200, content=raw.encode(), headers={"content-type": "application/json"}
                )
            ),
        )


def _no_key(exc: BaseException, caplog: pytest.LogCaptureFixture) -> None:
    assert KEY not in str(exc)
    assert KEY not in repr(exc)
    assert exc.__cause__ is None or KEY not in repr(exc.__cause__)
    assert KEY not in caplog.text
