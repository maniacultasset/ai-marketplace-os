"""Reset OTel's set-once global tracer provider around a test (S23 T14): the worker sets it, and
the SDK refuses a second `set_tracer_provider` in one process."""

from __future__ import annotations

from collections.abc import Iterator

import pytest


@pytest.fixture
def otel_global() -> Iterator[None]:
    from opentelemetry import trace
    from opentelemetry.util._once import Once

    saved = (trace._TRACER_PROVIDER, trace._TRACER_PROVIDER_SET_ONCE)
    trace._TRACER_PROVIDER = None
    trace._TRACER_PROVIDER_SET_ONCE = Once()
    try:
        yield
    finally:
        trace._TRACER_PROVIDER, trace._TRACER_PROVIDER_SET_ONCE = saved
