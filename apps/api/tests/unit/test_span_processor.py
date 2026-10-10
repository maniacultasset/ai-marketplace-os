"""S23 T14 (AC7, AC10; C1): the tracer accessor, the `request_id` span processor, and the worker
(only) setting the OTel global provider."""

from __future__ import annotations

from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from tests.support.otel_global import otel_global

__all__ = ["otel_global"]


def _settings(**values: Any) -> Any:
    from app.core.config import Settings

    base: dict[str, Any] = {"app_env": "local", "otel_traces_sampler": "always_on"}
    return Settings(_env_file=None, **{**base, **values})


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    from app.core.observability import init_tracing

    out = InMemorySpanExporter()
    init_tracing(_settings(), exporter=out)
    return out


def test_span_gets_request_id_inside_context(exporter: InMemorySpanExporter) -> None:
    from app.core.observability import get_tracer
    from app.core.request_context import request_context

    with request_context("rid-s23"), get_tracer().start_as_current_span("x"):
        pass
    [span] = exporter.get_finished_spans()
    assert span.attributes is not None and span.attributes["request_id"] == "rid-s23"


def test_span_gets_no_request_id_outside(exporter: InMemorySpanExporter) -> None:
    from app.core.observability import get_tracer

    with get_tracer().start_as_current_span("x"):
        pass
    [span] = exporter.get_finished_spans()
    assert "request_id" not in (span.attributes or {})


def test_nested_span_same_id(exporter: InMemorySpanExporter) -> None:
    from app.core.observability import get_tracer
    from app.core.request_context import request_context

    tracer = get_tracer()
    with (
        request_context("rid-nest"),
        tracer.start_as_current_span("outer"),
        tracer.start_as_current_span("inner"),
    ):
        pass
    spans = exporter.get_finished_spans()
    assert [s.name for s in spans] == ["inner", "outer"]
    assert {(s.attributes or {}).get("request_id") for s in spans} == {"rid-nest"}
    assert spans[0].context.trace_id == spans[1].context.trace_id


def test_get_tracer_noop_before_init(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import tracer as holder

    monkeypatch.setattr(holder, "_provider", None)
    tracer = holder.get_tracer()
    with tracer.start_as_current_span("x") as span:
        assert not span.is_recording()


def test_processor_registered_before_exporter() -> None:
    from app.core.observability import RequestIdSpanProcessor, init_tracing

    provider = init_tracing(_settings(), exporter=InMemorySpanExporter())
    processors = provider._active_span_processor._span_processors
    assert isinstance(processors[0], RequestIdSpanProcessor)


async def _run_until_queue(monkeypatch: pytest.MonkeyPatch) -> list[TracerProvider]:
    """`app.workers.main.run()` up to the queue app (which raises), capturing the provider."""
    from app.core import observability
    from app.workers import main

    built: list[TracerProvider] = []
    original = observability.init_tracing

    def capture(settings: Any, **kwargs: Any) -> TracerProvider:
        provider = original(settings, **kwargs)
        built.append(provider)
        return provider

    class StopError(Exception):
        pass

    def no_queue() -> Any:
        raise StopError

    monkeypatch.setattr(main, "init_tracing", capture)
    monkeypatch.setattr(main, "init_sentry", lambda _s: False)
    monkeypatch.setattr(main, "configure_logging", lambda _s: None)
    monkeypatch.setattr(main, "get_queue_app", no_queue)
    with pytest.raises(StopError):
        await main.run()
    return built


async def test_worker_run_inits_tracing(
    otel_global: None, monkeypatch: pytest.MonkeyPatch, ci_env: dict[str, str]
) -> None:
    built = await _run_until_queue(monkeypatch)
    assert len(built) == 1


async def test_worker_run_sets_global_provider(
    otel_global: None, monkeypatch: pytest.MonkeyPatch, ci_env: dict[str, str]
) -> None:
    built = await _run_until_queue(monkeypatch)
    assert trace.get_tracer_provider() is built[0]


def test_api_init_tracing_never_sets_global_provider(otel_global: None, make_app: Any) -> None:
    from app.core.observability import init_tracing

    init_tracing(_settings())
    make_app()
    assert not isinstance(trace.get_tracer_provider(), TracerProvider)
