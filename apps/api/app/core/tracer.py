"""The process's tracer provider holder (S23 C1), kept apart from `app.core.observability` so
`app.db` and `app.adapters` can open spans without importing Sentry or the request middleware.

`init_tracing` stores the provider it builds (`set_provider`); `get_tracer(name)` returns a
tracer from it, or a no-op tracer before any provider exists.
"""

from __future__ import annotations

from typing import Final

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

TRACER_NAME: Final = "app"
_provider: TracerProvider | None = None


def set_provider(provider: TracerProvider | None) -> None:
    global _provider
    _provider = provider


def get_tracer(name: str = TRACER_NAME) -> trace.Tracer:
    if _provider is None:
        return trace.NoOpTracer()
    return _provider.get_tracer(name)
