"""S23 T18 (AC7, AC10; C7): one CLIENT span per SP-API try, opened in the adapter's client, with
an allowlisted attribute set; tokens, headers, query strings and bodies never reach a span."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode
from tests.adapters.amazon_support import SELLER, FakeTokens, spapi_client
from tests.support.fake_marketplace import CANARY, build_fake
from tests.support.fault_transport import FaultTransport

BODY = {"productType": "KURTA", "requirements": "LISTING_PRODUCT_ONLY", "attributes": {"item_name": [{"value": "Zq9bodycanary"}]}}  # fmt: skip
ALLOWED = {
    "marketplace",
    "adapter.operation",
    "http.request.method",
    "url.template",
    "http.response.status_code",
    "duration_ms",
    "error_code",
}


@pytest.fixture
def traced() -> Iterator[tuple[TracerProvider, InMemorySpanExporter]]:
    from app.core.config import Settings
    from app.core.observability import init_tracing

    exporter = InMemorySpanExporter()
    settings = Settings(_env_file=None, app_env="local", otel_traces_sampler="always_on")
    yield init_tracing(settings, exporter=exporter), exporter


async def _control(app: Any, **body: Any) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://fake.test"
    ) as client:
        await client.post("/__control", json=body)


def _adapter(exporter: InMemorySpanExporter) -> list[ReadableSpan]:
    return [s for s in exporter.get_finished_spans() if s.name.startswith("adapter ")]


async def test_adapter_span_attributes_allowlist(
    traced: tuple[TracerProvider, InMemorySpanExporter],
) -> None:
    provider, exporter = traced
    app = build_fake()
    with provider.get_tracer("t").start_as_current_span("job") as parent:
        await spapi_client(app).put("SKU-1", BODY)
    [span] = _adapter(exporter)
    assert span.name == "adapter amazon.put" and span.kind is SpanKind.CLIENT
    assert span.parent is not None and span.parent.span_id == parent.get_span_context().span_id
    attrs = dict(span.attributes or {})
    assert set(attrs) == ALLOWED - {"error_code"}
    assert attrs["url.template"] == "/listings/2021-08-01/items/{sellerId}/{sku}"
    assert (attrs["marketplace"], attrs["adapter.operation"]) == ("amazon", "put")
    assert (attrs["http.request.method"], attrs["http.response.status_code"]) == ("PUT", 200)
    assert span.status.status_code is StatusCode.UNSET


async def test_adapter_span_error_on_4xx_with_code(
    traced: tuple[TracerProvider, InMemorySpanExporter],
) -> None:
    from app.adapters.base import PermissionDenied

    provider, exporter = traced
    app = build_fake()
    await _control(app, spapi_auth={"mode": "denied", "times": 5})
    with provider.get_tracer("t").start_as_current_span("job"), pytest.raises(PermissionDenied):
        await spapi_client(app).put("SKU-1", BODY)
    first, second = _adapter(exporter)  # the forced refresh retries once
    assert first.status.status_code is StatusCode.ERROR
    assert dict(first.attributes or {})["http.response.status_code"] == 403
    assert "error_code" not in (first.attributes or {})
    attrs = dict(second.attributes or {})
    assert attrs["error_code"] == "ACCOUNT_PERMISSION_DENIED"
    assert second.status.status_code is StatusCode.ERROR
    assert set(attrs) <= ALLOWED


async def test_adapter_span_error_on_raised_ambiguous(
    traced: tuple[TracerProvider, InMemorySpanExporter],
) -> None:
    from app.adapters.base import MarketplaceAmbiguous

    provider, exporter = traced
    app = build_fake()
    transport = FaultTransport(httpx.ASGITransport(app), after_write=httpx.ReadTimeout("t"))
    with provider.get_tracer("t").start_as_current_span("job"), pytest.raises(MarketplaceAmbiguous):
        await spapi_client(app, transport=transport).patch("SKU-1", {"patches": []})
    [span] = _adapter(exporter)
    assert span.status.status_code is StatusCode.ERROR
    assert dict(span.attributes or {})["error_code"] == "MARKETPLACE_UNCONFIRMED"
    assert [(e.name, dict(e.attributes or {})) for e in span.events] == [
        ("exception", {"exception.type": "MarketplaceAmbiguous"})
    ]


async def test_adapter_span_one_span_per_try(
    traced: tuple[TracerProvider, InMemorySpanExporter],
) -> None:
    provider, exporter = traced
    app = build_fake()
    await _control(app, spapi_429=1)
    with provider.get_tracer("t").start_as_current_span("job"):
        await spapi_client(app).put("SKU-1", BODY)
    spans = _adapter(exporter)
    assert [dict(s.attributes or {})["http.response.status_code"] for s in spans] == [429, 200]
    assert [s.status.status_code for s in spans] == [StatusCode.ERROR, StatusCode.UNSET]


async def test_adapter_span_none_without_parent(
    traced: tuple[TracerProvider, InMemorySpanExporter],
) -> None:
    _, exporter = traced
    await spapi_client(build_fake()).put("SKU-1", BODY)
    assert _adapter(exporter) == []


async def test_spans_never_leak_adapter(
    traced: tuple[TracerProvider, InMemorySpanExporter],
) -> None:
    provider, exporter = traced
    app = build_fake()
    tokens = FakeTokens(app)
    with provider.get_tracer("t").start_as_current_span("job"):
        client = spapi_client(app, tokens=tokens)
        await client.put("SKU-LEAK-1", BODY)
        await client.get("SKU-LEAK-1")
    assert tokens.token.startswith(CANARY)
    dumped = "\n".join(s.to_json() for s in exporter.get_finished_spans())
    for secret in (
        CANARY,
        "x-amz-access-token",
        "Authorization",
        "marketplaceIds",
        "includedData",
        "?",
        "Zq9bodycanary",
        "SKU-LEAK-1",
        SELLER,
    ):
        assert secret not in dumped, secret
