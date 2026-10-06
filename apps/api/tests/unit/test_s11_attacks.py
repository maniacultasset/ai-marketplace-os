"""S11 tester attacks on the observability spine through a real `create_app()`: the request id on
early-exit answers (413), probe look-alikes, tenant binding from untrusted input, secrets in
exception text and logger extras, and the Sentry event of an unhandled exception."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.conftest import LogCapture, SentryCapture

Start = Callable[..., LogCapture]

PLANTED_PASSWORD = "Zq9dsnpw"
PLANTED_TOKEN = "Zq9bearertoken"


def _server_lines(capture: LogCapture) -> list[dict[str, Any]]:
    return [line for line in capture.lines() if line["module"] not in {"httpx", "httpcore"}]


def _routes(app: FastAPI) -> FastAPI:
    async def dsn_boom() -> None:
        raise RuntimeError(
            f"connect to postgresql+psycopg://app:{PLANTED_PASSWORD}@db:5432/mos failed; "
            f"header was Bearer {PLANTED_TOKEN}"
        )

    async def extras() -> dict[str, bool]:
        logging.getLogger("app.tests.extras").warning(
            "outbound call",
            extra={
                "headers": [("Authorization", f"Bearer {PLANTED_TOKEN}"), ("Accept", "x")],
                "X-Amz-Access-Token": "Zq9amz",
                "client_secret": "Zq9client",
                "tokens_in": 12,
            },
        )
        return {"ok": True}

    app.add_api_route("/__t/dsn-boom", dsn_boom, methods=["GET"])
    app.add_api_route("/__t/extras", extras, methods=["GET"])
    return app


@pytest.fixture
def app(make_app: Callable[[], FastAPI]) -> FastAPI:
    return _routes(make_app())


def test_attack_413_carries_request_id_and_envelope(
    make_app: Callable[[], FastAPI], monkeypatch: pytest.MonkeyPatch, log_lines: Start
) -> None:
    monkeypatch.setenv("REQUEST_MAX_BODY_BYTES", "64")
    client = TestClient(make_app(), raise_server_exceptions=False)
    capture = log_lines()
    response = client.post("/api/v1/products", content=b"x" * 65, headers={"X-Request-ID": "big-1"})
    assert response.status_code == 413
    assert response.headers.get_list("x-request-id") == ["big-1"]
    assert response.json()["error"]["correlation_id"] == "big-1"
    access = [line for line in capture.lines() if line["module"] == "app.access"]
    assert [(line["status"], line["request_id"]) for line in access] == [(413, "big-1")]


@pytest.mark.parametrize("path", ["/healthz?verbose=1", "/readyz?x=Zq9q"])
def test_attack_probe_with_query_not_logged(
    app: FastAPI, log_lines: Start, fake_engine: Callable[..., Any], path: str
) -> None:
    fake_engine()
    capture = log_lines()
    response = TestClient(app).get(path, headers={"X-Request-ID": "probe-q"})
    assert response.headers["x-request-id"] == "probe-q"
    assert [line for line in capture.lines() if line["module"] == "app.access"] == []


@pytest.mark.parametrize("path", ["/healthz/", "/HEALTHZ", "//healthz"])
def test_attack_probe_lookalikes_are_logged(app: FastAPI, log_lines: Start, path: str) -> None:
    capture = log_lines()
    response = TestClient(app).get(path)
    assert response.status_code == 404
    assert len([line for line in capture.lines() if line["module"] == "app.access"]) == 1


def test_attack_tenant_never_bound_from_headers_or_query(app: FastAPI, log_lines: Start) -> None:
    capture = log_lines()
    TestClient(app).get(
        "/api/v1/nope?tenant_id=evil",
        headers={"X-Tenant-ID": "evil", "X-Request-ID": "t-1", "tid": "evil"},
    )
    lines = _server_lines(capture)
    assert lines
    assert all("tenant_id" not in line for line in lines), lines
    assert "evil" not in "\n".join(map(str, lines))


@pytest.mark.parametrize("request_id", ["a", "x" * 128, "a:b.c-d_e", "0000"])
def test_attack_request_id_charset_edges_kept(app: FastAPI, request_id: str) -> None:
    response = TestClient(app).get("/api/v1/nope", headers={"X-Request-ID": request_id})
    assert response.headers.get_list("x-request-id") == [request_id]
    assert response.json()["error"]["correlation_id"] == request_id


def test_attack_secret_in_exception_text_not_logged_or_returned(
    app: FastAPI, log_lines: Start
) -> None:
    capture = log_lines()
    response = TestClient(app, raise_server_exceptions=False).get(
        "/__t/dsn-boom", headers={"X-Request-ID": "boom-1"}
    )
    assert response.status_code == 500
    assert response.headers["x-request-id"] == "boom-1"
    errors = [line for line in _server_lines(capture) if line["severity"] == "ERROR"]
    assert len(errors) == 1
    assert errors[0]["request_id"] == "boom-1"
    assert "RuntimeError" in errors[0]["exception"]
    for leak in (PLANTED_PASSWORD, PLANTED_TOKEN):
        assert leak not in capture.text()
        assert leak not in response.text


def test_attack_stdlib_extras_redacted(app: FastAPI, log_lines: Start) -> None:
    capture = log_lines()
    TestClient(app).get("/__t/extras", headers={"X-Request-ID": "ex-1"})
    [line] = [line for line in capture.lines() if line["module"] == "app.tests.extras"]
    assert line["request_id"] == "ex-1"
    assert line["tokens_in"] == 12
    assert line["headers"][1] == ["Accept", "x"]
    for leak in (PLANTED_TOKEN, "Zq9amz", "Zq9client"):
        assert leak not in capture.text()


def test_attack_sentry_event_for_secret_exception_text(
    make_app: Callable[[], FastAPI],
    monkeypatch: pytest.MonkeyPatch,
    sentry_events: SentryCapture,
) -> None:
    monkeypatch.setenv("SENTRY_DSN", "https://public@o0.ingest.sentry.io/0")
    app = _routes(make_app())
    TestClient(app, raise_server_exceptions=False).get(
        "/__t/dsn-boom?code=Zq9oauthcode",
        headers={"X-Request-ID": "sentry-1", "Authorization": f"Bearer {PLANTED_TOKEN}"},
    )
    import sentry_sdk

    sentry_sdk.flush(timeout=2)
    events = sentry_events.events()
    assert len(events) == 1
    event = events[0]
    assert event["tags"]["request_id"] == "sentry-1"
    text = sentry_events.serialized()
    for leak in (PLANTED_PASSWORD, PLANTED_TOKEN, "Zq9oauthcode"):
        assert leak not in text
