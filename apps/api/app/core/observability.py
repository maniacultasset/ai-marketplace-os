"""Tracing and error monitoring, built only from `Settings` (ENV.md observability rows).

- `init_tracing()` builds a `TracerProvider` whose service name, sampler and ratio come from
  Settings; the SDK's environment autoconfiguration (`Resource.create()`, `OTEL_*` env) is never
  used. No exporter is installed yet (trace export is decided with billing at S4/S24); tests pass
  an in-memory one. The global provider is never set: the request middleware gets its tracer
  from the returned provider.
- With an active span and `GCP_PROJECT_ID` set, log lines carry Cloud Logging's trace fields.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.sampling import (
    ALWAYS_OFF,
    ALWAYS_ON,
    ParentBased,
    Sampler,
    TraceIdRatioBased,
)

from app.core.config import OtelSampler, Settings
from app.core.logging import set_trace_fields_hook

TRACER_NAME: Final = "app"
TRACE_FIELD: Final = "logging.googleapis.com/trace"
SPAN_ID_FIELD: Final = "logging.googleapis.com/spanId"
TRACE_SAMPLED_FIELD: Final = "logging.googleapis.com/trace_sampled"


def sampler_for(name: OtelSampler, ratio: float) -> Sampler:
    """The SDK sampler that the `OTEL_TRACES_SAMPLER` name means, with `ratio` as its argument."""
    match name:
        case "always_on":
            return ALWAYS_ON
        case "always_off":
            return ALWAYS_OFF
        case "traceidratio":
            return TraceIdRatioBased(ratio)
        case "parentbased_always_on":
            return ParentBased(ALWAYS_ON)
        case "parentbased_always_off":
            return ParentBased(ALWAYS_OFF)
        case "parentbased_traceidratio":
            return ParentBased(TraceIdRatioBased(ratio))


def trace_log_fields(project_id: str | None) -> Mapping[str, object] | None:
    """Cloud Logging's trace fields for the current span, or None without a valid span or a
    project id."""
    if not project_id:
        return None
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return None
    return {
        TRACE_FIELD: f"projects/{project_id}/traces/{trace.format_trace_id(context.trace_id)}",
        SPAN_ID_FIELD: trace.format_span_id(context.span_id),
        TRACE_SAMPLED_FIELD: context.trace_flags.sampled,
    }


def init_tracing(settings: Settings, *, exporter: SpanExporter | None = None) -> TracerProvider:
    provider = TracerProvider(
        resource=Resource({"service.name": settings.otel_service_name}),
        sampler=sampler_for(settings.otel_traces_sampler, settings.otel_traces_sampler_arg),
    )
    if exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    project_id = settings.gcp_project_id
    set_trace_fields_hook(lambda: trace_log_fields(project_id))
    return provider


def tracer_from(provider: TracerProvider) -> trace.Tracer:
    return provider.get_tracer(TRACER_NAME)
