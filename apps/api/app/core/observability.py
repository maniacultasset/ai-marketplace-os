"""Tracing and error monitoring, built only from `Settings` (ENV.md observability rows).

- `init_sentry()` starts Sentry only when `SENTRY_DSN` is set (Settings requires it outside
  `local`): `environment` from APP_ENV, `release` from GIT_SHA, explicit Starlette, FastAPI and
  SQLAlchemy integrations (no auto-enabling ones), no PII, no request bodies, no frame locals,
  no trace headers on outgoing requests, and every event, transaction and breadcrumb passed
  through `redact()`. Request query strings never leave (the OAuth callback carries the code).
- `init_tracing()` builds a `TracerProvider` whose service name, sampler and ratio come from
  Settings; the SDK's environment autoconfiguration (`Resource.create()`, `OTEL_*` env) is never
  used. No exporter is installed yet (trace export is decided with billing at S4/S24); tests pass
  an in-memory one. `init_tracing` never sets the OTel global provider: the request middleware
  gets its tracer from the returned provider. The worker (only) sets it afterwards (S23 C1), so
  code that calls `trace.get_tracer()` nests under the job span.
- `get_tracer(name)` (S23): a tracer from the provider the last `init_tracing` built (a no-op
  tracer before any), for spans opened below the request or job span (DB, adapter, job).
- `RequestIdSpanProcessor` (S23): every span started while a request id is bound carries it as
  `request_id`, so one trace's spans all name their request.
- With an active span and `GCP_PROJECT_ID` set, log lines carry Cloud Logging's trace fields.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

import sentry_sdk
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.sampling import (
    ALWAYS_OFF,
    ALWAYS_ON,
    ParentBased,
    Sampler,
    TraceIdRatioBased,
)
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration
from sentry_sdk.transport import Transport
from sentry_sdk.utils import BadDsn, Dsn

from app.core.config import OtelSampler, Settings
from app.core.logging import set_trace_fields_hook
from app.core.middleware import PROBE_PATHS
from app.core.redaction import redact
from app.core.request_context import current_request_id
from app.core.traceparent import current_traceparent as current_traceparent
from app.core.traceparent import parse_traceparent as parse_traceparent
from app.core.tracer import get_tracer as get_tracer
from app.core.tracer import set_provider

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


class RequestIdSpanProcessor(SpanProcessor):
    """Stamps `request_id` on every span started while a request id is bound (S23 AC10)."""

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        request_id = current_request_id()
        if request_id is not None:
            span.set_attribute("request_id", request_id)

    def on_end(self, span: ReadableSpan) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def init_tracing(settings: Settings, *, exporter: SpanExporter | None = None) -> TracerProvider:
    provider = TracerProvider(
        resource=Resource({"service.name": settings.otel_service_name}),
        sampler=sampler_for(settings.otel_traces_sampler, settings.otel_traces_sampler_arg),
    )
    # before any exporter: a span is stamped before it can be exported
    provider.add_span_processor(RequestIdSpanProcessor())
    if exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    project_id = settings.gcp_project_id
    set_trace_fields_hook(lambda: trace_log_fields(project_id))
    set_provider(provider)
    return provider


def tracer_from(provider: TracerProvider) -> trace.Tracer:
    return provider.get_tracer(TRACER_NAME)


# ---- Sentry ----

DEFAULT_TRANSPORT: Transport | None = None
"""Tests set a capturing transport here; None means the SDK's own HTTP transport."""


def _strip_query(event: dict[str, Any]) -> None:
    request = event.get("request")
    if not isinstance(request, dict):
        return
    request.pop("query_string", None)
    request.pop("data", None)
    request.pop("cookies", None)
    url = request.get("url")
    if isinstance(url, str) and "?" in url:
        request["url"] = url.split("?", 1)[0]


def _scrub(event: Any) -> Any:
    if not isinstance(event, dict):
        return event
    _strip_query(event)
    cleaned: dict[str, Any] = redact(event)
    request_id = current_request_id()
    if request_id is not None:
        tags = cleaned.get("tags")
        if not isinstance(tags, dict):
            tags = {}
        tags.setdefault("request_id", request_id)
        cleaned["tags"] = tags
    return cleaned


def before_send(event: Any, hint: Any) -> Any:
    return _scrub(event)


def before_send_transaction(event: Any, hint: Any) -> Any:
    return _scrub(event)


def before_breadcrumb(crumb: Any, hint: Any) -> Any:
    return redact(crumb)


def traces_sampler_for(rate: float) -> Any:
    """Probes are never traced; every other request gets the Settings rate. An inbound parent's
    sampling decision (`parent_sampled`) is ignored: callers are untrusted."""

    def sampler(context: Mapping[str, Any]) -> float:
        scope = context.get("asgi_scope")
        if isinstance(scope, Mapping) and scope.get("path") in PROBE_PATHS:
            return 0.0
        return rate

    return sampler


def init_sentry(settings: Settings, *, transport: Transport | None = None) -> bool:
    """Start Sentry from Settings. False (and no `sentry_sdk.init`) when SENTRY_DSN is unset."""
    dsn = settings.sentry_dsn
    if dsn is None:
        return False
    if not _valid_dsn(dsn):
        # Raised outside any `except`, so the BadDsn (whose text can quote the DSN) is unreachable.
        raise SystemExit("FATAL: refusing to boot. SENTRY_DSN is not a valid Sentry DSN")
    sentry_sdk.init(
        dsn=dsn,
        environment=settings.app_env,
        release=settings.git_sha,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        traces_sampler=traces_sampler_for(settings.sentry_traces_sample_rate),
        send_default_pii=False,
        max_request_body_size="never",
        include_local_variables=False,
        auto_enabling_integrations=False,
        integrations=[StarletteIntegration(), FastApiIntegration(), SqlalchemyIntegration()],
        trace_propagation_targets=[],
        before_send=before_send,
        before_send_transaction=before_send_transaction,
        before_breadcrumb=before_breadcrumb,
        transport=transport if transport is not None else DEFAULT_TRANSPORT,
    )
    return True


def _valid_dsn(dsn: str) -> bool:
    try:
        Dsn(dsn)
    except BadDsn:
        return False
    return True


def current_trace_id() -> str | None:
    """The current span's 32-hex trace id, or None without a valid span."""
    context = trace.get_current_span().get_span_context()
    return trace.format_trace_id(context.trace_id) if context.is_valid else None


def tag_request(request_id: str) -> None:
    """The request middleware's hook: tag the request's Sentry isolation scope with its
    `request_id` and (S23) the SERVER span's `trace_id`, so an event leads to its trace."""
    scope = sentry_sdk.get_isolation_scope()
    scope.set_tag("request_id", request_id)
    trace_id = current_trace_id()
    if trace_id is not None:
        scope.set_tag("trace_id", trace_id)
