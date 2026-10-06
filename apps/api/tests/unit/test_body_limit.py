"""S3 T3a (AC16): REQUEST_MAX_BODY_BYTES enforced before parsing or auth, declared or streamed."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from app.api.body_limit import BodyLimitMiddleware
from app.api.errors import ErrorEnvelope, install_error_handlers
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from mkos_schemas.codes import ErrorCode

CAP = 1024


def _app(cap: int = CAP) -> FastAPI:
    # MkosApp: the request middleware owns the request id and the `X-Request-ID` header (S11).
    from app.core.asgi import MkosApp

    app = MkosApp()
    install_error_handlers(app)

    @app.post("/echo-size")
    async def echo_size(request: Request) -> dict[str, int]:
        return {"size": len(await request.body())}

    @app.post("/json")
    async def json_body(payload: dict[str, Any]) -> dict[str, int]:
        return {"keys": len(payload)}

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.add_middleware(BodyLimitMiddleware, max_bytes=cap)
    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(_app(), raise_server_exceptions=False)


def _chunks(total: int, size: int = 256) -> Iterator[bytes]:
    sent = 0
    while sent < total:
        part = min(size, total - sent)
        sent += part
        yield b"x" * part


def _assert_envelope(response: Any, status: int, code: ErrorCode) -> None:
    assert response.status_code == status
    body = response.json()
    ErrorEnvelope.model_validate(body)
    assert body["error"]["type"] == code.value
    assert response.headers["x-request-id"] == body["error"]["correlation_id"]


def test_exactly_the_cap_passes(client: TestClient) -> None:
    response = client.post("/echo-size", content=b"x" * CAP)
    assert response.status_code == 200
    assert response.json() == {"size": CAP}


def test_cap_plus_one_by_content_length(client: TestClient) -> None:
    _assert_envelope(
        client.post("/echo-size", content=b"x" * (CAP + 1)), 413, ErrorCode.PAYLOAD_TOO_LARGE
    )


def test_cap_plus_one_streamed_without_content_length(client: TestClient) -> None:
    response = client.post("/echo-size", content=_chunks(CAP + 1))
    _assert_envelope(response, 413, ErrorCode.PAYLOAD_TOO_LARGE)


def test_streamed_json_over_cap(client: TestClient) -> None:
    def body() -> Iterator[bytes]:
        yield b'{"a": "'
        yield from _chunks(CAP)
        yield b'"}'

    response = client.post("/json", content=body(), headers={"content-type": "application/json"})
    _assert_envelope(response, 413, ErrorCode.PAYLOAD_TOO_LARGE)


def test_streamed_under_cap_passes(client: TestClient) -> None:
    response = client.post("/echo-size", content=_chunks(CAP))
    assert response.status_code == 200
    assert response.json() == {"size": CAP}


async def _drive(app: Any, headers: list[tuple[bytes, bytes]], chunks: list[bytes]) -> list[Any]:
    """Run the ASGI app directly so a lying Content-Length reaches it unchanged."""
    sent: list[Any] = []
    queue = [
        {"type": "http.request", "body": c, "more_body": i < len(chunks) - 1}
        for i, c in enumerate(chunks)
    ]

    async def receive() -> Any:
        return queue.pop(0) if queue else {"type": "http.disconnect"}

    async def send(message: Any) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/echo-size",
        "raw_path": b"/echo-size",
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 1),
        "server": ("testserver", 80),
    }
    await app(scope, receive, send)
    return sent


async def test_lying_small_content_length_with_large_stream() -> None:
    sent = await _drive(_app(), [(b"content-length", b"10")], [b"x" * 600, b"x" * 600])
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 413
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    assert b"PAYLOAD_TOO_LARGE" in body
    assert sum(1 for m in sent if m["type"] == "http.response.start") == 1


@pytest.mark.parametrize(
    "headers",
    [
        [(b"content-length", b"abc")],
        [(b"content-length", b"-1")],
        [(b"content-length", b"+5")],
        [(b"content-length", b"1.5")],
        [(b"content-length", b"5"), (b"content-length", b"5")],
    ],
)
async def test_bad_content_length_400(headers: list[tuple[bytes, bytes]]) -> None:
    sent = await _drive(_app(), headers, [b"hello"])
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 400
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    ErrorEnvelope.model_validate_json(body)
    assert b"VALIDATION_FAILED" in body


def test_get_healthz_unaffected(client: TestClient) -> None:
    assert client.get("/healthz").status_code == 200


ELEVEN_MIB = 11 * 1024 * 1024


def test_body_cap_bulk_unauthenticated(make_app: Callable[[], FastAPI]) -> None:
    client = TestClient(make_app(), raise_server_exceptions=False)
    files = {"file": ("big.csv", b"a" * ELEVEN_MIB, "text/csv")}
    response = client.post("/api/v1/products/bulk", files=files)
    _assert_envelope(response, 413, ErrorCode.PAYLOAD_TOO_LARGE)


def test_body_cap_bulk_unauthenticated_streamed(make_app: Callable[[], FastAPI]) -> None:
    client = TestClient(make_app(), raise_server_exceptions=False)
    headers = {"content-type": "multipart/form-data; boundary=b"}
    body = (b'--b\r\nContent-Disposition: form-data; name="file"; filename="a.csv"\r\n\r\n',)
    response = client.post(
        "/api/v1/products/bulk",
        content=iter([*body, *_chunks(ELEVEN_MIB, 1024 * 1024)]),
        headers=headers,
    )
    _assert_envelope(response, 413, ErrorCode.PAYLOAD_TOO_LARGE)


def test_body_cap_reads_setting(
    make_app: Callable[[], FastAPI], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REQUEST_MAX_BODY_BYTES", "100")
    client = TestClient(make_app(), raise_server_exceptions=False)
    over = client.post("/api/v1/products", content=b"x" * 101)
    _assert_envelope(over, 413, ErrorCode.PAYLOAD_TOO_LARGE)
    at_cap = client.post("/api/v1/products", content=b"x" * 100)
    assert at_cap.status_code != 413
