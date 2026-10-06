"""S11 T8: the SERVER span opened by the request middleware, sampling from Settings, no inbound
trace continuation, and the Cloud Logging trace fields."""

from __future__ import annotations

import secrets
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import (
    ALWAYS_OFF,
    ALWAYS_ON,
    ParentBased,
    TraceIdRatioBased,
)
from opentelemetry.trace import SpanKind, StatusCode
from tests.conftest import LogCapture

Start = Callable[..., LogCapture]
SAMPLERS = (
    "always_on",
    "always_off",
    "traceidratio",
    "parentbased_always_on",
    "parentbased_always_off",
    "parentbased_traceidratio",
)


def _settings(**overrides: Any) -> Any:
    from app.core.config import Settings

    values: dict[str, Any] = {"app_env": "local", "app_role": "shell"}
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def traced(
    make_app: Callable[[], FastAPI], exporter: InMemorySpanExporter
) -> Iterator[Callable[..., FastAPI]]:
    from app.core.logging import set_trace_fields_hook

    def build(**settings: Any) -> FastAPI:
        from app.core.observability import init_tracing, tracer_from

        app = make_app()
        values = {"otel_traces_sampler": "always_on", **settings}
        app.tracer = tracer_from(init_tracing(_settings(**values), exporter=exporter))  # type: ignore[attr-defined]

        async def item(item_id: int) -> dict[str, int]:
            from app.core.logging import get_logger

            get_logger("app.tests.traced").info("in span")
            return {"item_id": item_id}

        async def boom() -> None:
            raise RuntimeError("Bearer Zq9span")

        async def headers(request: Request) -> dict[str, str]:
            return dict(request.headers)

        app.add_api_route("/items/{item_id}", item, methods=["GET"])
        app.add_api_route("/boom", boom, methods=["GET"])
        app.add_api_route("/headers", headers, methods=["GET"])
        return app

    yield build
    set_trace_fields_hook(None)


def _spans(exporter: InMemorySpanExporter) -> list[ReadableSpan]:
    return list(exporter.get_finished_spans())


def test_otel_server_span(traced: Callable[..., FastAPI], exporter: InMemorySpanExporter) -> None:
    response = TestClient(traced()).get("/items/7", headers={"X-Request-ID": "abc"})
    assert response.status_code == 200
    [span] = _spans(exporter)
    assert span.kind is SpanKind.SERVER
    assert span.name == "GET /items/{item_id}"
    assert span.resource.attributes["service.name"] == "marketplace-os-api"
    assert span.attributes is not None
    assert span.attributes["request_id"] == "abc"
    assert span.attributes["http.route"] == "/items/{item_id}"
    assert span.attributes["http.request.method"] == "GET"
    assert span.attributes["http.response.status_code"] == 200
    assert span.parent is None


def test_otel_service_name_from_settings(
    traced: Callable[..., FastAPI], exporter: InMemorySpanExporter
) -> None:
    TestClient(traced(otel_service_name="marketplace-os-worker")).get("/items/1")
    [span] = _spans(exporter)
    assert span.resource.attributes["service.name"] == "marketplace-os-worker"


def test_otel_inbound_traceparent_ignored(
    traced: Callable[..., FastAPI], exporter: InMemorySpanExporter, log_lines: Start
) -> None:
    app = traced(gcp_project_id="p")
    capture = log_lines()
    inbound_trace = secrets.token_hex(16)
    TestClient(app).get(
        "/items/7", headers={"traceparent": f"00-{inbound_trace}-{secrets.token_hex(8)}-01"}
    )
    [span] = _spans(exporter)
    own = trace.format_trace_id(span.context.trace_id)
    assert own != inbound_trace
    assert span.parent is None
    [line] = [x for x in capture.lines() if x["module"] == "app.tests.traced"]
    assert line["logging.googleapis.com/trace"] == f"projects/p/traces/{own}"
    assert inbound_trace not in capture.text()


def test_otel_inbound_sampled_flag_ignored(
    traced: Callable[..., FastAPI], exporter: InMemorySpanExporter
) -> None:
    client = TestClient(
        traced(otel_traces_sampler="parentbased_traceidratio", otel_traces_sampler_arg=0.0)
    )
    for _ in range(100):
        parent = f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-01"
        client.get("/items/1", headers={"traceparent": parent, "tracestate": "x=1"})
    assert _spans(exporter) == []


def test_trace_headers_stripped_before_app(traced: Callable[..., FastAPI]) -> None:
    response = TestClient(traced()).get(
        "/headers",
        headers={
            "traceparent": f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-01",
            "TraceState": "a=b",
            "sentry-trace": f"{secrets.token_hex(16)}-{secrets.token_hex(8)}-1",
            "Baggage": "sentry-sample_rand=0.000001",
            "X-Request-ID": "abc",
            "X-Other": "kept",
        },
    )
    seen = response.json()
    for name in ("traceparent", "tracestate", "sentry-trace", "baggage"):
        assert name not in seen
    assert seen["x-request-id"] == "abc"
    assert seen["x-other"] == "kept"


def test_otel_span_error_on_500(
    traced: Callable[..., FastAPI], exporter: InMemorySpanExporter
) -> None:
    TestClient(traced(), raise_server_exceptions=False).get("/boom")
    [span] = _spans(exporter)
    assert span.status.status_code is StatusCode.ERROR
    assert span.attributes is not None
    assert span.attributes["http.response.status_code"] == 500
    [event] = [e for e in span.events if e.name == "exception"]
    assert event.attributes is not None
    assert event.attributes["exception.type"] == "RuntimeError"
    rendered = repr(span.to_json())
    assert "Zq9" not in rendered


@pytest.mark.parametrize("name", SAMPLERS)
def test_otel_sampler_from_settings(name: str) -> None:
    from app.core.observability import init_tracing

    provider = init_tracing(_settings(otel_traces_sampler=name, otel_traces_sampler_arg=0.25))
    sampler = provider.sampler
    expected = {
        "always_on": ALWAYS_ON,
        "always_off": ALWAYS_OFF,
    }
    if name in expected:
        assert sampler is expected[name]
    elif name == "traceidratio":
        assert isinstance(sampler, TraceIdRatioBased)
        assert sampler.rate == 0.25
    else:
        assert isinstance(sampler, ParentBased)
        description = sampler.get_description()  # type: ignore[no-untyped-call]
        if name == "parentbased_traceidratio":
            assert "TraceIdRatioBased{0.25}" in description
        elif name == "parentbased_always_on":
            assert "AlwaysOnSampler" in description
        else:
            assert "AlwaysOffSampler" in description


def test_otel_ignores_sdk_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.observability import init_tracing

    settings = _settings(otel_traces_sampler="always_on")
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "service.name=evil,deployment.environment=x")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "evil")
    monkeypatch.setenv("OTEL_TRACES_SAMPLER", "always_off")
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "otlp")
    provider = init_tracing(settings)
    assert provider.resource.attributes == {"service.name": "marketplace-os-api"}
    assert provider.sampler is ALWAYS_ON


def test_init_tracing_never_sets_global_provider() -> None:
    from app.core.observability import init_tracing

    before = trace.get_tracer_provider()
    provider = init_tracing(_settings())
    assert trace.get_tracer_provider() is before
    assert not isinstance(trace.get_tracer_provider(), TracerProvider) or before is not provider


@pytest.mark.parametrize("path", ["/healthz", "/readyz", "/healthz?x=1"])
def test_no_span_for_probes(
    traced: Callable[..., FastAPI],
    exporter: InMemorySpanExporter,
    fake_engine: Callable[..., Any],
    path: str,
) -> None:
    fake_engine()
    TestClient(traced()).get(path)
    assert _spans(exporter) == []


def test_span_for_lookalike_path(
    traced: Callable[..., FastAPI], exporter: InMemorySpanExporter
) -> None:
    TestClient(traced()).get("/api/v1/healthz")
    [span] = _spans(exporter)
    assert span.attributes is not None
    assert span.attributes["http.route"] == "<unmatched>"


@pytest.mark.parametrize("project", ["p", None, ""])
def test_log_trace_fields(log_lines: Start, project: str | None) -> None:
    from app.core.logging import get_logger, set_trace_fields_hook
    from app.core.observability import init_tracing, tracer_from

    settings = _settings(otel_traces_sampler="always_on", gcp_project_id=project)
    tracer = tracer_from(init_tracing(settings))
    capture = log_lines()
    try:
        get_logger("app.tests.trace").info("no span")
        with tracer.start_as_current_span("work") as span:
            get_logger("app.tests.trace").info("in span")
            context = span.get_span_context()
    finally:
        set_trace_fields_hook(None)
    outside, inside = capture.lines()
    assert "logging.googleapis.com/trace" not in outside
    if project:
        assert inside["logging.googleapis.com/trace"] == (
            f"projects/p/traces/{trace.format_trace_id(context.trace_id)}"
        )
        assert inside["logging.googleapis.com/spanId"] == trace.format_span_id(context.span_id)
        assert inside["logging.googleapis.com/trace_sampled"] is True
    else:
        assert "logging.googleapis.com/trace" not in inside
        assert "logging.googleapis.com/spanId" not in inside
