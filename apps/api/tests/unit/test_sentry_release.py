"""S23 T25 (AC13; D9): Sentry events carry the release (`GIT_SHA`) and the environment, and the
tags that lead to the trace: `request_id` and `trace_id` from a request; `request_id`, `trace_id`
and `job_kind` from a job, reset between jobs."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
import sentry_sdk
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

DSN = "https://public@o0.ingest.sentry.io/1"


@pytest.fixture
def app_with_release(
    make_app: Callable[[], FastAPI], monkeypatch: pytest.MonkeyPatch, sentry_events: Any
) -> FastAPI:
    from app.core.config import get_settings

    monkeypatch.setenv("SENTRY_DSN", DSN)
    monkeypatch.setenv("GIT_SHA", "0123abc")
    monkeypatch.setenv("OTEL_TRACES_SAMPLER", "always_on")
    get_settings.cache_clear()
    app = make_app()

    async def boom(request: Request) -> None:
        raise ValueError("boom")

    app.add_api_route("/boom", boom)
    return app


def test_sentry_release_api(app_with_release: FastAPI, sentry_events: Any) -> None:
    client = TestClient(app_with_release, raise_server_exceptions=False)
    response = client.get("/boom", headers={"X-Request-ID": "rid-release"})
    assert response.status_code == 500
    sentry_sdk.get_client().flush(timeout=2)
    [event] = sentry_events.events()
    assert event["release"] == "0123abc"
    assert event["environment"] == "local"
    assert event["tags"]["request_id"] == "rid-release"
    trace_id = event["tags"]["trace_id"]
    assert len(trace_id) == 32 and int(trace_id, 16) != 0


async def _job_event(kind: str, request_id: str) -> None:
    """What the wrapper does around one job: its isolation scope and tags, then an event."""
    from app.core.config import Settings
    from app.core.observability import init_tracing
    from app.core.request_context import request_context
    from app.core.tracer import get_tracer

    init_tracing(Settings(_env_file=None, app_env="local", otel_traces_sampler="always_on"))
    with request_context(request_id), get_tracer().start_as_current_span(f"job {kind}"):
        from app.core.observability import current_trace_id

        with sentry_sdk.isolation_scope() as scope:
            scope.set_tag("request_id", request_id)
            scope.set_tag("job_kind", kind)
            scope.set_tag("trace_id", current_trace_id() or "")
            sentry_sdk.capture_message(f"in {kind}")


@pytest.fixture
def worker_sentry(sentry_events: Any) -> Any:
    from app.core.config import Settings
    from app.core.observability import init_sentry

    init_sentry(
        Settings(
            _env_file=None, app_env="local", app_role="shell", sentry_dsn=DSN, git_sha="0123abc"
        )
    )
    return sentry_events


async def test_sentry_release_worker(worker_sentry: Any) -> None:
    await _job_event("bulk_import", "rid-job")
    sentry_sdk.get_client().flush(timeout=2)
    [event] = worker_sentry.events()
    assert event["release"] == "0123abc"
    assert event["tags"]["request_id"] == "rid-job"
    assert event["tags"]["job_kind"] == "bulk_import"
    assert len(event["tags"]["trace_id"]) == 32


async def test_sentry_worker_tags_reset_between_jobs(worker_sentry: Any) -> None:
    await _job_event("bulk_import", "rid-one")
    await _job_event("brand_embed", "rid-two")
    sentry_sdk.capture_message("after the jobs")
    sentry_sdk.get_client().flush(timeout=2)
    one, two, after = worker_sentry.events()
    assert (one["tags"]["job_kind"], two["tags"]["job_kind"]) == ("bulk_import", "brand_embed")
    assert one["tags"]["trace_id"] != two["tags"]["trace_id"]
    assert "job_kind" not in after.get("tags", {})
    assert "trace_id" not in after.get("tags", {})


def test_wrapper_opens_one_isolation_scope_per_job() -> None:
    """The wrapper's own code: an isolation scope with the three tags around each job."""
    import inspect

    from app.workers import wrapper

    source = inspect.getsource(wrapper.run_job)
    assert "sentry_sdk.isolation_scope()" in source
    for tag in ('"request_id"', '"job_kind"', '"trace_id"'):
        assert f"sentry_scope.set_tag({tag}" in source
