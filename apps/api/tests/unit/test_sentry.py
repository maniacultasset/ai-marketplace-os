"""S11 T9: Sentry from Settings, explicit integrations, and redaction of every event."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from typing import Any

import pytest
import sentry_sdk
from fastapi import FastAPI, Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from tests.conftest import LogCapture, SentryCapture

DSN = "https://public@o0.ingest.example.invalid/0"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJacTkifQ.Zq9signature"


def _settings(**overrides: Any) -> Any:
    from app.core.config import Settings

    values: dict[str, Any] = {"app_env": "local", "app_role": "shell", "sentry_dsn": DSN}
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.fixture
def sentry_app(
    make_app: Callable[[], FastAPI], monkeypatch: pytest.MonkeyPatch, sentry_events: SentryCapture
) -> Callable[..., FastAPI]:
    """`create_app()` with a DSN (so Sentry starts through the app factory) and test routes."""

    def build(**env: str) -> FastAPI:
        from app.core.config import get_settings

        monkeypatch.setenv("SENTRY_DSN", DSN)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()
        app = make_app()

        async def boom(request: Request) -> None:
            sentry_sdk.add_breadcrumb(
                category="auth", message="login with Bearer Zq9crumb", data={"password": "Zq9p"}
            )
            sentry_sdk.set_extra("access_token", "Zq9extra")
            sentry_sdk.set_context("lwa", {"refresh_token": "Zq9ctx", "region": "eu"})
            raise ValueError(f"Bearer Zq9exc {JWT}")

        async def login(request: Request) -> None:
            await request.body()
            raise RuntimeError("login failed")

        async def callback() -> None:
            raise RuntimeError("callback failed")

        async def ok() -> dict[str, bool]:
            return {"ok": True}

        app.add_api_route("/boom", boom, methods=["GET"])
        app.add_api_route("/login", login, methods=["POST"])
        # Ahead of the real (stub) callback route, so this test fixture route answers it.
        app.router.routes.insert(
            0, APIRoute("/api/v1/marketplaces/amazon/callback", callback, methods=["GET"])
        )
        app.add_api_route("/api/v1/x", ok, methods=["GET"])
        return app

    return build


def _client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def test_sentry_init_empty_dsn_no_client(
    monkeypatch: pytest.MonkeyPatch, sentry_events: SentryCapture
) -> None:
    from app.core import observability

    calls: list[Any] = []
    monkeypatch.setattr(sentry_sdk, "init", lambda **kw: calls.append(kw))
    monkeypatch.setenv("SENTRY_DSN", DSN)  # in os.environ, but not what Settings loaded
    assert observability.init_sentry(_settings(sentry_dsn=None)) is False
    assert observability.init_sentry(_settings(sentry_dsn="")) is False
    assert calls == []
    assert not sentry_sdk.get_client().is_active()


def test_sentry_init_options(sentry_events: SentryCapture) -> None:
    from app.core.observability import init_sentry

    assert init_sentry(
        _settings(app_env="staging", git_sha="abc1234", sentry_traces_sample_rate=0.25)
    )
    options = sentry_sdk.get_client().options
    assert options["environment"] == "staging"
    assert options["release"] == "abc1234"
    assert options["traces_sample_rate"] == 0.25
    assert options["send_default_pii"] is False
    assert options["max_request_body_size"] == "never"
    assert options["include_local_variables"] is False
    assert options["auto_enabling_integrations"] is False
    assert list(options["trace_propagation_targets"]) == []


def test_sentry_init_integrations(sentry_events: SentryCapture) -> None:
    from app.core.observability import init_sentry

    init_sentry(_settings())
    installed = set(sentry_sdk.get_client().integrations)
    assert {"starlette", "fastapi", "sqlalchemy"} <= installed
    for unwanted in ("redis", "celery", "arq", "httpx", "openai", "langchain", "asyncpg"):
        assert unwanted not in installed


@pytest.mark.parametrize("dsn", ["https://Zq9secret@x/", "not a dsn Zq9", "ftp://Zq9@x/1"])
def test_sentry_bad_dsn_exits_without_value(sentry_events: SentryCapture, dsn: str) -> None:
    from app.core.observability import init_sentry

    with pytest.raises(SystemExit) as info:
        init_sentry(_settings(sentry_dsn=dsn))
    message = str(info.value.code)
    assert "SENTRY_DSN" in message
    assert "Zq9" not in message
    assert info.value.__context__ is None
    assert not sentry_sdk.get_client().is_active()


def test_redaction_sentry_request_headers(
    sentry_app: Callable[..., FastAPI], sentry_events: SentryCapture
) -> None:
    _client(sentry_app()).get(
        "/boom",
        headers={
            "Authorization": "Bearer Zq9auth",
            "Cookie": "session=Zq9cookie",
            "X-Api-Key": "Zq9key",
            "X-Request-ID": "abc",
        },
    )
    [event] = sentry_events.events()
    headers = {k.lower(): v for k, v in event["request"]["headers"].items()}
    assert headers["authorization"] == "[REDACTED]"
    assert headers.get("cookie", "[REDACTED]") == "[REDACTED]"
    assert headers["x-api-key"] == "[REDACTED]"
    assert "Zq9" not in sentry_events.serialized()


def test_redaction_sentry_extra_contexts_breadcrumbs(
    sentry_app: Callable[..., FastAPI], sentry_events: SentryCapture
) -> None:
    _client(sentry_app()).get("/boom")
    [event] = sentry_events.events()
    assert event["extra"]["access_token"] == "[REDACTED]"
    assert event["contexts"]["lwa"]["refresh_token"] == "[REDACTED]"
    assert event["contexts"]["lwa"]["region"] == "eu"
    crumbs = event["breadcrumbs"]["values"]
    auth = [c for c in crumbs if c.get("category") == "auth"]
    assert auth and auth[0]["data"]["password"] == "[REDACTED]"
    assert "Zq9" not in sentry_events.serialized()


def test_redaction_sentry_exception_value(
    sentry_app: Callable[..., FastAPI], sentry_events: SentryCapture
) -> None:
    _client(sentry_app()).get("/boom")
    [event] = sentry_events.events()
    [value] = event["exception"]["values"]
    assert value["type"] == "ValueError"
    assert "Zq9" not in value["value"]
    assert "eyJ" not in value["value"]


def test_sentry_event_has_no_body_locals_or_ip(
    sentry_app: Callable[..., FastAPI], sentry_events: SentryCapture
) -> None:
    _client(sentry_app()).post(
        "/login",
        json={"email": "a@b.example", "password": "Zq9pw"},
        headers={"X-Forwarded-For": "203.0.113.7"},
    )
    [event] = sentry_events.events()
    text = sentry_events.serialized()
    assert "Zq9" not in text
    assert "a@b" not in text
    assert "203.0.113.7" not in text
    assert "testclient" not in text  # the ASGI client host
    assert "data" not in event["request"]
    for value in event["exception"]["values"]:
        for frame in value.get("stacktrace", {}).get("frames", []):
            assert "vars" not in frame
    assert "ip_address" not in event.get("user", {}) or event["user"]["ip_address"] == "[REDACTED]"
    assert "REMOTE_ADDR" not in event["request"].get("env", {})


def test_sentry_unhandled_exception_one_event_with_request_id_tag(
    sentry_app: Callable[..., FastAPI],
    sentry_events: SentryCapture,
    log_lines: Callable[..., LogCapture],
) -> None:
    app = sentry_app()
    capture = log_lines()
    response = _client(app).get("/boom", headers={"X-Request-ID": "abc"})
    assert response.status_code == 500
    events = sentry_events.events()
    assert len(events) == 1
    assert events[0]["tags"]["request_id"] == "abc"
    errors = [line for line in capture.lines() if line["severity"] == "ERROR"]
    assert len(errors) == 1


def test_sentry_request_id_tag_not_leaked_across_requests(
    sentry_app: Callable[..., FastAPI], sentry_events: SentryCapture
) -> None:
    client = _client(sentry_app())
    client.get("/boom", headers={"X-Request-ID": "first"})
    client.get("/boom", headers={"X-Request-ID": "second"})
    assert [e["tags"]["request_id"] for e in sentry_events.events()] == ["first", "second"]


@pytest.mark.parametrize("path", ["/healthz", "/readyz"])
def test_sentry_probes_not_traced(path: str) -> None:
    from app.core.observability import traces_sampler_for

    sampler = traces_sampler_for(1.0)
    assert sampler({"asgi_scope": {"type": "http", "path": path}}) == 0.0
    assert sampler({"asgi_scope": {"type": "http", "path": f"/api/v1{path}"}}) == 1.0


@pytest.mark.parametrize("rate", [0.0, 0.1])
def test_sentry_sampler_ignores_parent_sampled(rate: float) -> None:
    from app.core.observability import traces_sampler_for

    sampler = traces_sampler_for(rate)
    context = {"parent_sampled": True, "asgi_scope": {"type": "http", "path": "/api/v1/x"}}
    assert sampler(context) == rate


def test_sentry_inbound_trace_never_continued(
    sentry_app: Callable[..., FastAPI], sentry_events: SentryCapture
) -> None:
    client = _client(sentry_app(SENTRY_TRACES_SAMPLE_RATE="0"))
    inbound = secrets.token_hex(16)
    for _ in range(100):
        client.get(
            "/api/v1/x",
            headers={
                "sentry-trace": f"{inbound}-{secrets.token_hex(8)}-1",
                "baggage": f"sentry-trace_id={inbound},sentry-sample_rand=0.000001,sentry-sampled=true",
            },
        )
    client.get("/boom", headers={"sentry-trace": f"{inbound}-{secrets.token_hex(8)}-1"})
    assert sentry_events.transactions() == []
    [event] = sentry_events.events()
    assert event["contexts"]["trace"]["trace_id"] != inbound
    assert inbound not in sentry_events.serialized()


def test_redaction_sentry_oauth_callback_query(
    sentry_app: Callable[..., FastAPI], sentry_events: SentryCapture
) -> None:
    _client(sentry_app()).get(
        "/api/v1/marketplaces/amazon/callback?spapi_oauth_code=Zq9&state=Zq8&mws_auth_token=Zq7"
    )
    [event] = sentry_events.events()
    text = sentry_events.serialized()
    assert "Zq" not in text
    assert "query_string" not in event["request"]
    assert "?" not in event["request"]["url"]
