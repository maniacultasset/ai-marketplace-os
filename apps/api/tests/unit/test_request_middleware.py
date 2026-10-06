"""S11 T6/T7/T12: the outermost request middleware (request id, access line, error paths)."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.types import Receive, Scope, Send
from tests.conftest import LogCapture

Start = Callable[..., LogCapture]


def _is_uuid4(value: str) -> bool:
    try:
        return uuid.UUID(value).version == 4
    except ValueError:
        return False


def _tenant_async() -> Any:
    from app.core.request_context import bind_tenant

    async def dep() -> None:
        bind_tenant("tenant-async")

    return dep


def _tenant_sync() -> Any:
    from app.core.request_context import bind_tenant

    def dep() -> None:
        bind_tenant("tenant-sync")

    return dep


def _add_test_routes(app: FastAPI) -> FastAPI:
    """Test-only routes on a real `create_app()` (never in production code)."""
    from app.core.logging import get_logger

    log = get_logger("app.tests.route")

    async def item(item_id: int) -> dict[str, int]:
        log.info("structlog in route")
        logging.getLogger("app.tests.stdlib").info("stdlib in route")
        logging.getLogger("sqlalchemy.engine").warning("sqlalchemy in route")
        return {"item_id": item_id}

    async def inner_id() -> JSONResponse:
        return JSONResponse({"ok": True}, headers={"X-Request-ID": "inner-value"})

    async def tenant_async(_: None = Depends(_tenant_async())) -> dict[str, bool]:
        return {"ok": True}

    def tenant_sync(_: None = Depends(_tenant_sync())) -> dict[str, bool]:
        return {"ok": True}

    async def tenant_n(n: int) -> dict[str, int]:
        from app.core.request_context import bind_tenant

        bind_tenant(f"tenant-{n}")
        await asyncio.sleep(0.001 * (n % 7))
        logging.getLogger("app.tests.concurrent").info("work %s", n)
        return {"n": n}

    async def boom() -> None:
        raise RuntimeError("Bearer Zq9boom")

    app.add_api_route("/items/{item_id}", item, methods=["GET"])
    app.add_api_route("/inner-id", inner_id, methods=["GET"])
    app.add_api_route("/tenant/async", tenant_async, methods=["GET"])
    app.add_api_route("/tenant/sync", tenant_sync, methods=["GET"])
    app.add_api_route("/tenant/n/{n}", tenant_n, methods=["GET"])
    app.add_api_route("/boom", boom, methods=["GET"])
    app.add_api_route("/body/{n}", item, methods=["POST"])
    return app


@pytest.fixture
def app(make_app: Callable[[], FastAPI]) -> FastAPI:
    return _add_test_routes(make_app())


def _client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=True)


def _server_lines(capture: LogCapture) -> list[dict[str, Any]]:
    """Lines the app wrote (the test client's own `httpx` lines are not the server's)."""
    return [line for line in capture.lines() if line["module"] not in {"httpx", "httpcore"}]


def _access_lines(capture: LogCapture) -> list[dict[str, Any]]:
    return [line for line in capture.lines() if line["module"] == "app.access"]


def test_request_id_propagates(app: FastAPI, log_lines: Start) -> None:
    capture = log_lines()
    response = _client(app).get("/items/7", headers={"X-Request-ID": "abc"})
    assert response.status_code == 200
    assert response.headers["x-request-id"] == "abc"
    lines = _server_lines(capture)
    modules = {line["module"] for line in lines}
    assert {"app.access", "app.tests.route", "app.tests.stdlib", "sqlalchemy.engine"} <= modules
    assert all(line["request_id"] == "abc" for line in lines), lines


def test_request_id_minted(app: FastAPI, log_lines: Start) -> None:
    capture = log_lines()
    response = _client(app).get("/items/7")
    minted = response.headers["x-request-id"]
    assert _is_uuid4(minted)
    [access] = _access_lines(capture)
    assert access["request_id"] == minted


@pytest.mark.parametrize(
    "raw", [b"a\r\nb", b"x" * 129, b"", b" ", b"abc def", b"\xc3\xa9t\xc3\xa9", b"<script>"]
)
async def test_request_id_invalid_replaced(app: FastAPI, log_lines: Start, raw: bytes) -> None:
    capture = log_lines()
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/items/7",
        "raw_path": b"/items/7",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"t"), (b"x-request-id", raw)],
        "client": ("127.0.0.1", 1),
        "server": ("t", 80),
        "state": {},
    }
    await app(scope, receive, send)  # type: ignore[arg-type]
    start = next(m for m in sent if m["type"] == "http.response.start")
    ids = [v for k, v in start["headers"] if k.lower() == b"x-request-id"]
    assert len(ids) == 1
    assert ids[0] != raw
    assert _is_uuid4(ids[0].decode())
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    if raw.strip():
        assert raw not in body
        assert raw.decode("latin-1") not in capture.text()
    assert all(line["request_id"] == ids[0].decode() for line in _server_lines(capture))


def test_two_request_id_headers_replaced(app: FastAPI) -> None:
    client = _client(app)
    response = client.get("/items/1", headers=[("X-Request-ID", "a"), ("X-Request-ID", "b")])
    assert _is_uuid4(response.headers["x-request-id"])


def test_single_request_id_header(app: FastAPI) -> None:
    response = _client(app).get("/inner-id", headers={"X-Request-ID": "abc"})
    assert response.headers.get_list("x-request-id") == ["abc"]


def test_access_line_fields(app: FastAPI, log_lines: Start) -> None:
    capture = log_lines()
    _client(app).get(
        "/items/42?token=Zq9query&x=1",
        headers={"X-Request-ID": "abc", "User-Agent": "Zq9agent", "X-Forwarded-For": "203.0.113.9"},
    )
    [access] = _access_lines(capture)
    assert access["message"] == "request"
    assert access["method"] == "GET"
    assert access["route"] == "/items/{item_id}"
    assert access["status"] == 200
    assert isinstance(access["duration_ms"], float)
    assert access["severity"] == "INFO"
    text = "\n".join(map(str, _server_lines(capture)))
    for leak in ("Zq9", "testclient", "203.0.113.9", "/items/42"):
        assert leak not in text


def test_access_line_unmatched_route(app: FastAPI, log_lines: Start) -> None:
    capture = log_lines()
    response = _client(app).get("/nope/123", headers={"X-Request-ID": "abc"})
    assert response.status_code == 404
    [access] = _access_lines(capture)
    assert access["route"] == "<unmatched>"
    assert access["status"] == 404


@pytest.mark.parametrize("path", ["/healthz", "/readyz"])
def test_probes_not_logged(
    app: FastAPI, log_lines: Start, fake_engine: Callable[..., Any], path: str
) -> None:
    fake_engine()
    capture = log_lines()
    response = _client(app).get(path, headers={"X-Request-ID": "probe-1"})
    assert response.headers["x-request-id"] == "probe-1"
    assert _access_lines(capture) == []


def test_probe_lookalike_is_logged(app: FastAPI, log_lines: Start) -> None:
    capture = log_lines()
    _client(app).get("/api/v1/healthz")
    assert len(_access_lines(capture)) == 1


@pytest.mark.parametrize(
    ("path", "tenant"), [("/tenant/async", "tenant-async"), ("/tenant/sync", "tenant-sync")]
)
def test_bind_tenant_from_dependency(
    app: FastAPI, log_lines: Start, path: str, tenant: str
) -> None:
    capture = log_lines()
    _client(app).get(path, headers={"X-Request-ID": "abc"})
    [access] = _access_lines(capture)
    assert access["tenant_id"] == tenant


async def test_request_id_isolated_concurrent(app: FastAPI, log_lines: Start) -> None:
    from app.core.request_context import current_request_id

    capture = log_lines()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        responses = await asyncio.gather(
            *(client.get(f"/tenant/n/{n}", headers={"X-Request-ID": f"req-{n}"}) for n in range(50))
        )
    for n, response in enumerate(responses):
        assert response.headers["x-request-id"] == f"req-{n}"
        assert response.json() == {"n": n}
    lines = _server_lines(capture)
    assert len(lines) == 100
    for line in lines:
        n = int(line["request_id"].removeprefix("req-"))
        assert line["tenant_id"] == f"tenant-{n}"
        if line["module"] == "app.tests.concurrent":
            assert line["message"] == f"work {n}"
    assert current_request_id() is None


def test_lifespan_passes_through(app: FastAPI) -> None:
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200


async def test_websocket_and_lifespan_scopes_untouched() -> None:
    from app.core.middleware import RequestContextMiddleware
    from opentelemetry.trace import NoOpTracer

    seen: list[Scope] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope)

    middleware = RequestContextMiddleware(inner, tracer=NoOpTracer())
    scope: Scope = {"type": "lifespan"}

    async def nothing() -> Any:
        return {}

    async def drop(message: Any) -> None:
        return None

    await middleware(scope, nothing, drop)
    assert seen == [scope]


# ---- T7: error paths ----


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [
        ("GET", "/nope", 404),
        ("DELETE", "/items/1", 405),
        ("GET", "/items/x", 422),
        ("GET", "/boom", 500),
    ],
)
def test_request_id_on_error_responses(app: FastAPI, method: str, path: str, status: int) -> None:
    client = TestClient(app, raise_server_exceptions=False)
    response = client.request(method, path, headers={"X-Request-ID": "abc"})
    assert response.status_code == status
    assert response.headers.get_list("x-request-id") == ["abc"]


def test_unhandled_exception_logged_once_with_request_id(
    app: FastAPI, log_lines: Start, caplog: pytest.LogCaptureFixture
) -> None:
    capture = log_lines()
    response = _client(app).get("/boom", headers={"X-Request-ID": "abc"})
    assert response.status_code == 500
    lines = _server_lines(capture)
    errors = [line for line in lines if line["severity"] == "ERROR"]
    assert len(errors) == 1
    [error] = errors
    assert error["request_id"] == "abc"
    assert error["module"] == "app.request"
    assert "RuntimeError" in error["exception"]
    assert "Zq9" not in capture.text()
    assert all("request_id" in line for line in lines)
    # Nothing logged the traceback without the request id (no uvicorn-style line).
    assert [r for r in caplog.records if r.exc_info and r.name != "app.request"] == []


def test_unhandled_exception_access_line_status_500(app: FastAPI, log_lines: Start) -> None:
    capture = log_lines()
    _client(app).get("/boom", headers={"X-Request-ID": "abc"})
    [access] = _access_lines(capture)
    assert access["status"] == 500
    assert access["route"] == "/boom"


async def test_exception_before_response_start_still_500(log_lines: Start) -> None:
    from app.core.middleware import RequestContextMiddleware
    from opentelemetry.trace import NoOpTracer

    async def raw(scope: Scope, receive: Receive, send: Send) -> None:
        raise ValueError("Bearer Zq9raw")

    capture = log_lines()
    middleware = RequestContextMiddleware(raw, tracer=NoOpTracer())
    transport = httpx.ASGITransport(app=middleware)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        response = await client.get("/raw", headers={"X-Request-ID": "abc"})
    assert response.status_code == 500
    assert response.headers.get_list("x-request-id") == ["abc"]
    assert response.json()["error"]["type"] == "INTERNAL_ERROR"
    assert "Zq9" not in response.text
    assert "Zq9" not in capture.text()
    assert len([line for line in capture.lines() if line["severity"] == "ERROR"]) == 1


async def test_exception_after_response_start_not_resent(log_lines: Start) -> None:
    from app.core.middleware import RequestContextMiddleware
    from opentelemetry.trace import NoOpTracer

    async def half(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise ValueError("late")

    sent: list[dict[str, Any]] = []

    async def send(message: Any) -> None:
        sent.append(message)

    async def receive() -> Any:
        return {"type": "http.request", "body": b""}

    capture = log_lines()
    middleware = RequestContextMiddleware(half, tracer=NoOpTracer())
    await middleware({"type": "http", "method": "GET", "path": "/x", "headers": []}, receive, send)
    assert [m["type"] for m in sent] == ["http.response.start"]
    [access] = _access_lines(capture)
    assert access["status"] == 500


def test_context_reset_after_500(app: FastAPI) -> None:
    from app.core.request_context import current_request_id

    _client(app).get("/boom", headers={"X-Request-ID": "abc"})
    assert current_request_id() is None


# ---- T12: the S3 error envelope carries the request id ----


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [
        ("GET", "/nope", 404),
        ("DELETE", "/items/1", 405),
        ("GET", "/items/x", 422),
        ("GET", "/boom", 500),
    ],
)
def test_error_envelope_correlation_matches_request_id(
    app: FastAPI, log_lines: Start, method: str, path: str, status: int
) -> None:
    capture = log_lines()
    client = TestClient(app, raise_server_exceptions=False)
    response = client.request(method, path, headers={"X-Request-ID": "abc"})
    assert response.status_code == status
    body = response.json()["error"]
    assert body["correlation_id"] == "abc"
    assert body["error_report"]["request_id"] == "abc"
    assert response.headers.get_list("x-request-id") == ["abc"]
    [access] = _access_lines(capture)
    assert access["request_id"] == "abc"


@pytest.mark.parametrize("raw", ["a b", "x" * 129, "semi;colon"])
def test_error_envelope_invalid_header_not_echoed(app: FastAPI, raw: str) -> None:
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/nope", headers={"X-Request-ID": raw})
    minted = response.headers["x-request-id"]
    assert _is_uuid4(minted)
    assert response.json()["error"]["correlation_id"] == minted
    assert raw not in response.text
