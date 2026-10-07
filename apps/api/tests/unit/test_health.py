"""AC6 (a)-(c), AC7: the probes, the readiness cache and the docs routes."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

UNAVAILABLE = {"status": "unavailable", "checks": {"postgres": "unavailable"}}
READY = {"status": "ok", "checks": {"postgres": "ok"}}
OK = {"status": "ok"}
PRIVATE_JWT = "Zq9-not-committed-private-secret-32+"  # neither a Canon literal nor dev-prefixed
TEST_SENTRY_DSN = "https://public@o0.ingest.example.invalid/0"  # never resolvable (.invalid)
# S15: a deployed api also needs the token key and an https dashboard origin.
TEST_KMS_KEY_URI = (
    "projects/aimos-staging/locations/asia-south1/keyRings/marketplace-os/cryptoKeys/"
    "oauth-token-key"
)
TEST_DASHBOARD = "https://app.example.invalid"
TEST_REDIRECT = "https://api.example.invalid/api/v1/marketplaces/{}/callback"


def _client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=True)


class CountingPing:
    def __init__(self, result: bool = True, delay: float = 0.0) -> None:
        self.calls = 0
        self.result = result
        self.delay = delay

    async def __call__(self, timeout_seconds: float) -> bool:
        self.calls += 1
        await asyncio.sleep(self.delay)
        return self.result


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_healthz_200_body(make_app: Callable[[], FastAPI]) -> None:
    response = _client(make_app()).get("/healthz")
    assert response.status_code == 200
    assert response.json() == OK


def test_healthz_never_touches_db(
    make_app: Callable[[], FastAPI], monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode() -> Any:
        raise AssertionError("/healthz must not build or use the engine")

    monkeypatch.setattr("app.db.session.get_engine", explode)
    response = _client(make_app()).get("/healthz")
    assert response.status_code == 200
    assert response.json() == OK


def test_readyz_503_hides_driver_error(
    make_app: Callable[[], FastAPI], fake_engine: Callable[..., Any]
) -> None:
    fake_engine(error=RuntimeError("connection failed: host=db user=postgres password=Zq9"))
    response = _client(make_app()).get("/readyz")
    assert response.status_code == 503
    assert response.json() == UNAVAILABLE
    assert "Zq9" not in response.text


def test_readyz_503_on_timeout(
    make_app: Callable[[], FastAPI],
    fake_engine: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.services.health.READINESS_TIMEOUT_SECONDS", 0.05)
    fake_engine(delay=5.0)
    response = _client(make_app()).get("/readyz")
    assert response.status_code == 503
    assert response.json() == UNAVAILABLE


def test_readyz_200_with_fake_engine(
    make_app: Callable[[], FastAPI], fake_engine: Callable[..., Any]
) -> None:
    engine = fake_engine()
    response = _client(make_app()).get("/readyz")
    assert response.status_code == 200
    assert response.json() == READY
    assert engine.executed == ["SELECT 1"]


@pytest.mark.parametrize("path", ["/healthz", "/readyz"])
def test_probes_are_get_only(make_app: Callable[[], FastAPI], path: str) -> None:
    assert _client(make_app()).post(path).status_code == 405


@pytest.mark.parametrize("app_env", ["local", "staging"])
def test_docs_and_openapi_served(
    ci_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, app_env: str, sentry_events: Any
) -> None:
    from app.core.config import get_settings
    from app.main import create_app

    monkeypatch.setenv("APP_ENV", app_env)
    monkeypatch.setenv("JWT_SECRET", PRIVATE_JWT)
    monkeypatch.setenv("SENTRY_DSN", TEST_SENTRY_DSN)
    monkeypatch.setenv("KMS_KEY_URI", TEST_KMS_KEY_URI)
    monkeypatch.setenv("DASHBOARD_BASE_URL", TEST_DASHBOARD)
    monkeypatch.setenv("AMAZON_OAUTH_REDIRECT_URI", TEST_REDIRECT.format("amazon"))
    monkeypatch.setenv("FLIPKART_OAUTH_REDIRECT_URI", TEST_REDIRECT.format("flipkart"))
    get_settings.cache_clear()
    client = _client(create_app())
    assert client.get("/docs").status_code == 200
    spec = client.get("/openapi.json")
    assert spec.status_code == 200
    assert {"/healthz", "/readyz"} <= set(spec.json()["paths"])


def test_redoc_disabled(make_app: Callable[[], FastAPI]) -> None:
    assert _client(make_app()).get("/redoc").status_code == 404


def test_create_app_builds_no_engine(make_app: Callable[[], FastAPI]) -> None:
    from app.db.session import get_engine

    get_engine.cache_clear()
    make_app()
    assert get_engine.cache_info().currsize == 0


def test_trailing_slash_not_redirected(make_app: Callable[[], FastAPI]) -> None:
    app = make_app()

    async def probe() -> dict[str, str]:
        return OK

    app.add_api_route("/api/v1/probe", probe, methods=["GET"])
    client = _client(app)
    for path in ("/healthz/", "/api/v1/probe/"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 404, path
        assert "location" not in response.headers
    assert client.get("/api/v1/probe").status_code == 200


async def test_readyz_single_flight_20_concurrent_one_ping(
    make_app: Callable[[], FastAPI], monkeypatch: pytest.MonkeyPatch
) -> None:
    ping = CountingPing(result=True, delay=0.05)
    monkeypatch.setattr("app.services.health.ping", ping)
    transport = httpx.ASGITransport(app=make_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        responses = await asyncio.gather(*(client.get("/readyz") for _ in range(20)))
    assert [r.status_code for r in responses] == [200] * 20
    assert ping.calls == 1


def _with_clock(app: FastAPI, clock: FakeClock) -> FastAPI:
    from app.services.health import ReadinessCheck

    app.state.readiness = ReadinessCheck(clock=clock)
    return app


def test_readyz_result_cached_within_ttl_then_rechecked(
    make_app: Callable[[], FastAPI], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.health import READINESS_CACHE_SECONDS

    ping, clock = CountingPing(result=True), FakeClock()
    monkeypatch.setattr("app.services.health.ping", ping)
    client = _client(_with_clock(make_app(), clock))
    assert client.get("/readyz").status_code == 200
    clock.now += READINESS_CACHE_SECONDS / 2
    assert client.get("/readyz").status_code == 200
    assert ping.calls == 1
    clock.now += READINESS_CACHE_SECONDS
    assert client.get("/readyz").status_code == 200
    assert ping.calls == 2


def test_readyz_failure_cached_within_ttl(
    make_app: Callable[[], FastAPI], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.health import READINESS_CACHE_SECONDS

    ping, clock = CountingPing(result=False), FakeClock()
    monkeypatch.setattr("app.services.health.ping", ping)
    client = _client(_with_clock(make_app(), clock))
    for _ in range(5):
        response = client.get("/readyz")
        assert response.status_code == 503
        assert response.json() == UNAVAILABLE
    assert ping.calls == 1
    clock.now += READINESS_CACHE_SECONDS * 2
    assert client.get("/readyz").status_code == 503
    assert ping.calls == 2


async def test_readiness_bounded_while_an_overrunning_check_drains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC6b, fix round 1 (F1): a hung established connection makes psycopg's cancel-then-close
    outlive `ping`'s own timeout by ~10 s. Every probe still answers within the bound, and probes
    arriving during the drain join the same check instead of starting new pings."""
    from app.services.health import READINESS_CACHE_SECONDS, ReadinessCheck

    bound = 0.05
    monkeypatch.setattr("app.services.health.READINESS_TIMEOUT_SECONDS", bound)
    ping, clock = CountingPing(result=True, delay=bound * 10), FakeClock()
    monkeypatch.setattr("app.services.health.ping", ping)
    check = ReadinessCheck(clock=clock)
    loop = asyncio.get_running_loop()

    for _ in range(2):  # the probe that starts the check, then one that joins it mid-drain
        started = loop.time()
        assert await check.is_ready() is False
        assert loop.time() - started < bound * 4
        clock.now += READINESS_CACHE_SECONDS * 2  # the real drain (~10 s) outlasts the cache
    assert ping.calls == 1

    await asyncio.sleep(bound * 10)  # the check drains and records its own late result
    assert await check.is_ready() is True
    assert ping.calls == 1


async def test_bound_expiring_as_the_check_finishes_keeps_its_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F1 guard: when the bound expires in the same loop pass as the check finishes, the probe
    reports the check's result and never overwrites the cached result with a false failure."""
    from app.services.health import ReadinessCheck

    calls = 0

    async def instant_ping(timeout_seconds: float) -> bool:
        nonlocal calls
        calls += 1
        return True  # no await: the check task finishes in its first step

    # A zero bound makes wait_for cancel its shield first and only then see the finished check.
    monkeypatch.setattr("app.services.health.READINESS_TIMEOUT_SECONDS", 0.0)
    monkeypatch.setattr("app.services.health.ping", instant_ping)
    check = ReadinessCheck(clock=FakeClock())
    assert await check.is_ready() is True
    assert await check.is_ready() is True  # served from the cache
    assert calls == 1


def test_docs_and_openapi_off_in_production(
    clean_env: object, monkeypatch: pytest.MonkeyPatch, sentry_events: Any
) -> None:
    from app.core.config import get_settings
    from app.main import create_app

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("JWT_SECRET", PRIVATE_JWT)
    monkeypatch.setenv("SENTRY_DSN", TEST_SENTRY_DSN)
    monkeypatch.setenv("KMS_KEY_URI", TEST_KMS_KEY_URI)
    monkeypatch.setenv("DASHBOARD_BASE_URL", TEST_DASHBOARD)
    monkeypatch.setenv("AMAZON_OAUTH_REDIRECT_URI", TEST_REDIRECT.format("amazon"))
    monkeypatch.setenv("FLIPKART_OAUTH_REDIRECT_URI", TEST_REDIRECT.format("flipkart"))
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://app_rw:pw@127.0.0.1:5432/x")
    get_settings.cache_clear()
    app = create_app()
    client = _client(app)
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/healthz").status_code == 200
    assert isinstance(app.openapi(), dict)


@pytest.mark.parametrize(
    ("ready", "status", "body"), [(True, 200, READY), (False, 503, UNAVAILABLE)]
)
def test_health_readyz_names_postgres(
    make_app: Callable[[], FastAPI],
    monkeypatch: pytest.MonkeyPatch,
    ready: bool,
    status: int,
    body: dict[str, Any],
) -> None:
    monkeypatch.setattr("app.services.health.ping", CountingPing(result=ready))
    response = _client(make_app()).get("/readyz")
    assert response.status_code == status
    assert response.json() == body


def test_health_probes_no_access_line_no_span(
    make_app: Callable[[], FastAPI],
    fake_engine: Callable[..., Any],
    log_lines: Callable[..., Any],
) -> None:
    from app.core.config import Settings
    from app.core.observability import init_tracing, tracer_from
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    fake_engine()
    exporter = InMemorySpanExporter()
    app = make_app()
    settings = Settings(
        _env_file=None, app_env="local", app_role="shell", otel_traces_sampler="always_on"
    )
    app.tracer = tracer_from(init_tracing(settings, exporter=exporter))  # type: ignore[attr-defined]
    capture = log_lines()
    client = _client(app)
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200
    assert [line for line in capture.lines() if line["module"] == "app.access"] == []
    assert exporter.get_finished_spans() == ()
