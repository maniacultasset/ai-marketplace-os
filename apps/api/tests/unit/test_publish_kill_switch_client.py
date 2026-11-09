"""The publish kill switch at the SP-API client (`AMAZON_PUBLISH_ENABLED=false`): every write call
(PUT, PATCH and the VALIDATION_PREVIEW dry run) is refused before the limiter, the token or the
network, so the fake marketplace sees zero requests; every read still goes out."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from app.adapters.base import MarketplaceDisabled, PublishingPaused
from mkos_schemas.codes import ErrorCode
from tests.adapters.amazon_support import amazon_adapter, spapi_client
from tests.support.fake_marketplace import build_fake

from .test_amazon_adapter import _five

EAN = "8901234567890"
BODY = {"productType": "KURTA", "requirements": "LISTING", "attributes": {}}
PATCH = {"productType": "KURTA", "patches": []}


@pytest.fixture
def paused(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from app.core.config import get_settings

    monkeypatch.setenv("AMAZON_PUBLISH_ENABLED", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _writes(app: Any) -> list[dict[str, Any]]:
    return [c for c in app.state.fake.calls if c["method"] != "GET"]


def test_switch_is_on_in_code(clean_env: Any) -> None:
    from app.core.config import Settings

    plain = Settings(_env_file=None, app_env="local", jwt_secret="x" * 32)
    assert plain.amazon_publish_enabled is True


@pytest.mark.parametrize(
    ("call", "args"),
    [("put", ("SKU-1", BODY)), ("patch", ("SKU-1", PATCH)), ("put_preview", ("SKU-1", BODY))],
)
async def test_every_write_is_refused_before_anything(
    paused: None, call: str, args: tuple[Any, ...]
) -> None:
    app = build_fake()
    client = spapi_client(app)
    with pytest.raises(PublishingPaused) as raised:
        await getattr(client, call)(*args)
    assert isinstance(raised.value, MarketplaceDisabled)
    report = raised.value.report
    assert report.error_code is ErrorCode.NOT_IMPLEMENTED and report.retryable is False
    assert "paused" in report.errors[0].message
    assert app.state.fake.calls == []  # nothing reached the marketplace
    assert client._limiter.calls == []  # nor the limiter
    assert client.used_secrets() == []  # nor the token
    await client.aclose()


async def test_reads_still_go_out(paused: None) -> None:
    app = build_fake()
    client = spapi_client(app)
    got = await client.get("SKU-1")
    assert got.status == 404
    found = await client.search_catalog([EAN], "EAN")
    assert found.status == 200
    restricted = await client.get_restrictions("B0ABCDEF12")
    assert restricted.status == 200
    assert [c["method"] for c in app.state.fake.calls] == ["GET", "GET", "GET"]
    await client.aclose()


async def test_adapter_create_update_and_preview_send_nothing(paused: None) -> None:
    app = build_fake()
    adapter = amazon_adapter(app)
    sent = _five()
    with pytest.raises(PublishingPaused):
        await adapter.create_listing(sent)
    with pytest.raises(PublishingPaused):
        await adapter.publish_listing(sent)
    with pytest.raises(PublishingPaused):
        await adapter.update_listing(sent, fields=["content.title"])
    with pytest.raises(PublishingPaused):
        await adapter.preview_remote(sent)
    assert _writes(app) == []
    assert app.state.fake.submissions == 0
    await adapter.aclose()


async def test_switch_on_writes_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("AMAZON_PUBLISH_ENABLED", "true")
    get_settings.cache_clear()
    app = build_fake()
    client = spapi_client(app)
    got = await client.put("SKU-1", BODY)
    assert got.status == 200
    assert [c["method"] for c in app.state.fake.calls] == ["PUT"]
    await client.aclose()
    get_settings.cache_clear()
