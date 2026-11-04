"""ADR-0025 (EDGE-CF T2): the origin guard. Only a request carrying the Cloudflare Worker's
`x-kuinn-origin-auth` secret reaches the API under `ORIGIN_AUTH=enforce`; the header never
reaches the app, its logs or Sentry in any mode."""

from __future__ import annotations

import hmac
from collections.abc import Callable
from typing import Any

import pytest
from app.api.origin_auth import CLIENT_IP_HEADER, HEADER, SCOPE_KEY, OriginGuard, matches
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from starlette.types import Message
from tests.conftest import LogCapture

SECRET = "Zq9-edge-origin-secret-current-32"
PREVIOUS = "Zq9-edge-origin-secret-previous-3"
ME = "/api/v1/auth/me"
H = HEADER.decode()
IP = CLIENT_IP_HEADER.decode()

Factory = Callable[..., FastAPI]
Start = Callable[..., LogCapture]


def _probe_routes(app: FastAPI, seen: list[dict[str, Any]]) -> FastAPI:
    """A test-only route that records what the inner app saw."""

    async def inner(request: Request) -> dict[str, bool]:
        seen.append(
            {
                "names": [name for name, _ in request.scope["headers"]],
                "client_ip": request.headers.getlist(IP),
                "ok": request.scope.get(SCOPE_KEY),
            }
        )
        return {"ok": True}

    app.add_api_route("/api/v1/_probe", inner, methods=["GET", "POST"])
    return app


@pytest.fixture
def build(make_app: Callable[[], FastAPI], monkeypatch: pytest.MonkeyPatch) -> Factory:
    def factory(mode: str = "enforce", secret: str | None = SECRET) -> FastAPI:
        from app.core.config import get_settings

        monkeypatch.setenv("ORIGIN_AUTH", mode)
        if secret is None:
            monkeypatch.delenv("EDGE_ORIGIN_SECRET", raising=False)
        else:
            monkeypatch.setenv("EDGE_ORIGIN_SECRET", secret)
        get_settings.cache_clear()
        return make_app()

    return factory


def _client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=True)


def _assert_denied(response: Any) -> None:
    assert response.status_code == 403, response.text
    body = response.json()
    assert body["error"]["type"] == "PERMISSION_DENIED"
    assert body["error"]["error_report"]["error_code"] == "PERMISSION_DENIED"
    assert response.headers["x-request-id"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"


def _raw_get(client: TestClient, path: str, headers: list[tuple[Any, Any]]) -> Any:
    """A request carrying each header pair as given (a name may repeat)."""
    return client.send(client.build_request("GET", path, headers=headers))


# ---- happy path ----


def test_right_header_passes(build: Factory) -> None:
    client = _client(build())
    assert client.get(ME, headers={H: SECRET}).status_code == 401  # past the guard, no bearer


def test_previous_rotation_value_passes(build: Factory) -> None:
    client = _client(build(secret=f"{SECRET} {PREVIOUS}"))
    assert client.get(ME, headers={H: PREVIOUS}).status_code == 401
    assert client.get(ME, headers={H: SECRET}).status_code == 401


def test_header_name_case_is_ignored(build: Factory) -> None:
    client = _client(build())
    assert client.get(ME, headers={"X-Kuinn-Origin-Auth": SECRET}).status_code == 401


# ---- misses ----


@pytest.mark.parametrize(
    "value",
    [
        "",
        "Zq9-edge-origin-secret-current-33",  # same length, wrong value
        "x",
        "x" * 31,
        "x" * 33,
        "x" * 4096,
        SECRET + " ",
        SECRET.upper(),
        SECRET.swapcase(),
    ],
)
def test_wrong_value_denied(build: Factory, value: str) -> None:
    _assert_denied(_client(build()).get(ME, headers={H: value}))


def test_missing_header_denied(build: Factory) -> None:
    _assert_denied(_client(build()).get(ME))


def test_unicode_value_denied(build: Factory) -> None:
    client = _client(build())
    _assert_denied(_raw_get(client, ME, [(HEADER, ("é" * 32).encode())]))


@pytest.mark.parametrize("second", ["wrong", SECRET])
def test_header_twice_denied(build: Factory, second: str) -> None:
    client = _client(build())
    _assert_denied(_raw_get(client, ME, [(H, SECRET), (H, second)]))


def test_lookalike_header_name_denied(build: Factory) -> None:
    _assert_denied(_client(build()).get(ME, headers={"x-kuinn-origin-auth2": SECRET}))


def test_forged_verdict_header_denied(build: Factory) -> None:
    _assert_denied(_client(build()).get(ME, headers={SCOPE_KEY: "True"}))


# ---- coverage: before routing, CORS and the body ----


def test_unknown_path_denied_not_404(build: Factory) -> None:
    _assert_denied(_client(build()).get("/nope"))


def test_preflight_denied(build: Factory) -> None:
    response = _client(build()).options(
        ME,
        headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "GET"},
    )
    _assert_denied(response)


async def test_big_body_never_received(build: Factory) -> None:
    app = build()
    sent: list[Message] = []

    async def receive() -> Message:
        raise AssertionError("the guard read the body")

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/auth/login",
        "raw_path": b"/api/v1/auth/login",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-length", str(20 * 1024 * 1024).encode())],
        "client": ("203.0.113.9", 1234),
        "server": ("testserver", 80),
    }
    await app(scope, receive, send)
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 403


@pytest.mark.parametrize("path", ["/healthz", "/readyz"])
def test_probes_exempt(build: Factory, path: str) -> None:
    assert _client(build()).get(path).status_code != 403


@pytest.mark.parametrize("path", ["/readyz/", "/readyzX", "/healthz/x", "/healthzz"])
def test_probe_lookalikes_not_exempt(build: Factory, path: str) -> None:
    _assert_denied(_client(build()).get(path))


def test_probe_query_string_behaves_as_router(build: Factory) -> None:
    # the path is still `/healthz`: exempt exactly as the router matches it
    assert _client(build()).get("/healthz?x=1").status_code == 200


# ---- the header never reaches the app, logs or Sentry ----


@pytest.mark.parametrize("mode", ["enforce", "off"])
@pytest.mark.parametrize("value", [SECRET, "wrong"])
def test_header_stripped_before_the_app(build: Factory, mode: str, value: str) -> None:
    seen: list[dict[str, Any]] = []
    app = _probe_routes(build(mode), seen)
    response = _client(app).get("/api/v1/_probe", headers={H: value})
    if mode == "enforce" and value != SECRET:
        _assert_denied(response)
        assert seen == []
        return
    assert response.status_code == 200
    [inner] = seen
    assert HEADER not in inner["names"]


def test_scope_handed_to_sentry_layer_has_no_header(
    build: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = build()
    handed: list[Any] = []
    original = FastAPI.__call__

    async def spy(self: FastAPI, scope: Any, receive: Any, send: Any) -> None:
        handed.append(scope)
        await original(self, scope, receive, send)

    monkeypatch.setattr(FastAPI, "__call__", spy)
    client = _client(app)
    client.get(ME, headers={H: SECRET})
    client.get(ME, headers={H: "wrong"})
    http = [scope for scope in handed if scope["type"] == "http"]
    assert len(http) == 2
    for scope in http:
        assert all(name != HEADER for name, _ in scope["headers"])


def test_denied_log_line_holds_no_value(build: Factory, log_lines: Start) -> None:
    app = build()
    capture = log_lines()
    sent = "Zq9-sent-value-never-logged-0000000"
    _assert_denied(_client(app).get(ME, headers={H: sent}))
    denied = [line for line in capture.lines() if line.get("message") == "origin_auth.denied"]
    assert len(denied) == 1
    assert denied[0]["path"] == ME
    assert denied[0]["severity"] == "WARNING"
    text = capture.text()
    assert sent not in text
    assert SECRET not in text


# ---- off mode (AC5) ----


@pytest.mark.parametrize("path", [ME, "/nope", "/healthz"])
def test_off_without_header_unchanged(build: Factory, path: str) -> None:
    status = {ME: 401, "/nope": 404, "/healthz": 200}[path]
    assert _client(build("off", secret=None)).get(path).status_code == status
    assert _client(build("off")).get(path).status_code == status


@pytest.mark.parametrize(("value", "verdict"), [(SECRET, True), ("wrong", False), (None, False)])
def test_off_marks_the_verdict(build: Factory, value: str | None, verdict: bool) -> None:
    seen: list[dict[str, Any]] = []
    app = _probe_routes(build("off"), seen)
    headers = {H: value} if value is not None else {}
    assert _client(app).get("/api/v1/_probe", headers=headers).status_code == 200
    assert seen[0]["ok"] is verdict


# ---- the edge client-IP header (AC12) ----


@pytest.mark.parametrize("mode", ["off", "enforce"])
def test_client_ip_kept_only_with_a_match(build: Factory, mode: str) -> None:
    seen: list[dict[str, Any]] = []
    client = _client(_probe_routes(build(mode), seen))
    client.get("/api/v1/_probe", headers={H: SECRET, IP: "203.0.113.5"})
    assert seen[-1]["client_ip"] == ["203.0.113.5"]
    if mode == "off":
        client.get("/api/v1/_probe", headers={H: "wrong", IP: "203.0.113.5"})
        assert seen[-1]["client_ip"] == []
        client.get("/api/v1/_probe", headers={IP: "203.0.113.5"})
        assert seen[-1]["client_ip"] == []
    _raw_get(client, "/api/v1/_probe", [(H, SECRET), (IP, "203.0.113.5"), (IP, "198.51.100.1")])
    assert seen[-1]["client_ip"] == []


def test_client_ip_stripped_with_no_secret_configured(build: Factory) -> None:
    seen: list[dict[str, Any]] = []
    client = _client(_probe_routes(build("off", secret=None), seen))
    client.get("/api/v1/_probe", headers={H: "", IP: "203.0.113.5"})
    assert seen[-1]["client_ip"] == []
    assert seen[-1]["ok"] is False


# ---- matches() ----


def test_matches_compares_every_value(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[bytes, bytes]] = []
    real = hmac.compare_digest

    def counting(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(hmac, "compare_digest", counting)
    assert matches(b"a" * 32, (b"a" * 32, b"b" * 32)) is True
    assert len(calls) == 2


def test_matches_no_secrets_is_false() -> None:
    assert matches(b"", ()) is False
    assert matches(SECRET.encode(), ()) is False


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=200)
@given(st.binary(max_size=128))
def test_matches_only_the_secret(value: bytes) -> None:
    secrets = (SECRET.encode(), PREVIOUS.encode())
    assert matches(value, secrets) is (value in secrets)
    assert matches(SECRET.encode(), secrets) is True


def test_screen_leaves_lifespan_alone() -> None:
    guard = OriginGuard(enforce=True, secrets=(SECRET.encode(),))
    scope = {"type": "lifespan"}
    assert guard.screen(scope) is scope


def test_lifespan_runs_under_enforce(build: Factory) -> None:
    with _client(build()) as client:
        _assert_denied(client.get(ME))
