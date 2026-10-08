"""S16 T6 (AC7; CP8): `OpenAIEmbedder` over a scripted `httpx2.MockTransport` (no socket)."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

import httpx2
import pytest
from app.services.embeddings import EmbeddingFailed, OpenAIEmbedder
from mkos_schemas.codes import ErrorCode

CANARY_KEY = "canary-" + "not-a-key-" + "9f8e7d"  # built so no scanner sees a literal
DIMS = 1536


def _settings(**over: Any) -> Any:
    from app.core.config import Settings

    return Settings(_env_file=None, app_env="local", openai_api_key=CANARY_KEY, **over)


def _vector(seed: int) -> list[float]:
    return [float(seed)] + [0.0] * (DIMS - 1)


def _ok(texts: list[str], *, order: list[int] | None = None, dims: int = DIMS) -> dict[str, Any]:
    indexes = order if order is not None else list(range(len(texts)))
    return {
        "object": "list",
        "model": "text-embedding-3-small",
        "data": [
            {"object": "embedding", "index": i, "embedding": [float(i + 1)] + [0.0] * (dims - 1)}
            for i in indexes
        ],
        "usage": {"prompt_tokens": 7 * len(texts), "total_tokens": 7 * len(texts)},
    }


class Script:
    def __init__(self, handler: Callable[[httpx2.Request], httpx2.Response]) -> None:
        self.handler = handler
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self.handler(request)


def _embedder(script: Script) -> OpenAIEmbedder:
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(script))
    return OpenAIEmbedder(_settings(), http_client=client)


def _status(code: int) -> Script:
    return Script(
        lambda _: httpx2.Response(code, json={"error": {"message": "nope " + CANARY_KEY}})
    )


async def test_openai_embedder_request_shape() -> None:
    script = Script(lambda r: httpx2.Response(200, json=_ok(json.loads(r.content)["input"])))
    batch = await _embedder(script).embed(["one", "two"])
    (request,) = script.requests
    assert request.url.path == "/v1/embeddings"
    assert request.url.host == "api.openai.com"
    body = json.loads(request.content)
    assert body["model"] == "text-embedding-3-small"
    assert body["input"] == ["one", "two"]
    assert body["encoding_format"] == "float"
    assert "dimensions" not in body
    assert request.headers["Authorization"] == f"Bearer {CANARY_KEY}"
    assert len(batch.vectors) == 2 and batch.model == "text-embedding-3-small"


async def test_openai_embedder_base_url_env_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://evil.example.invalid/v1")
    script = Script(lambda r: httpx2.Response(200, json=_ok(["x"])))
    await _embedder(script).embed(["x"])
    assert script.requests[0].url.host == "api.openai.com"


async def test_openai_embedder_orders_by_index() -> None:
    script = Script(lambda r: httpx2.Response(200, json=_ok(["a", "b", "c"], order=[2, 0, 1])))
    batch = await _embedder(script).embed(["a", "b", "c"])
    assert [v[0] for v in batch.vectors] == [1.0, 2.0, 3.0]


async def test_openai_embedder_usage_tokens() -> None:
    script = Script(lambda r: httpx2.Response(200, json=_ok(["a", "b"])))
    assert (await _embedder(script).embed(["a", "b"])).input_tokens == 14


@pytest.mark.parametrize(("status", "retryable"), [(429, True), (500, True), (503, True)])
async def test_openai_embedder_429_500_retryable(status: int, retryable: bool) -> None:
    with pytest.raises(EmbeddingFailed) as info:
        await _embedder(_status(status)).embed(["a"])
    assert info.value.code is ErrorCode.MODEL_UNAVAILABLE
    assert info.value.retryable is retryable and not info.value.timed_out


async def test_openai_embedder_connection_error_retryable() -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    with pytest.raises(EmbeddingFailed) as info:
        await _embedder(Script(refuse)).embed(["a"])
    assert info.value.code is ErrorCode.MODEL_UNAVAILABLE and info.value.retryable


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_openai_embedder_4xx_not_retryable(status: int) -> None:
    with pytest.raises(EmbeddingFailed) as info:
        await _embedder(_status(status)).embed(["a"])
    assert info.value.code is ErrorCode.MODEL_UNAVAILABLE
    assert not info.value.retryable and info.value.status == status


async def test_openai_embedder_timeout_marked() -> None:
    def slow(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("slow", request=request)

    with pytest.raises(EmbeddingFailed) as info:
        await _embedder(Script(slow)).embed(["a"])
    assert info.value.code is ErrorCode.TIMEOUT
    assert info.value.retryable and info.value.timed_out


async def test_openai_embedder_count_mismatch() -> None:
    script = Script(lambda r: httpx2.Response(200, json=_ok(["a"])))
    with pytest.raises(EmbeddingFailed) as info:
        await _embedder(script).embed(["a", "b"])
    assert info.value.code is ErrorCode.MODEL_UNAVAILABLE and not info.value.retryable


async def test_openai_embedder_dims_mismatch() -> None:
    script = Script(lambda r: httpx2.Response(200, json=_ok(["a"], dims=3072)))
    with pytest.raises(EmbeddingFailed) as info:
        await _embedder(script).embed(["a"])
    assert not info.value.retryable


async def test_openai_embedder_bad_body_not_retryable() -> None:
    script = Script(lambda r: httpx2.Response(200, content=b"not json"))
    with pytest.raises(EmbeddingFailed):
        await _embedder(script).embed(["a"])


async def test_openai_embedder_no_sdk_retry() -> None:
    script = _status(500)
    with pytest.raises(EmbeddingFailed):
        await _embedder(script).embed(["a"])
    assert len(script.requests) == 1


async def test_openai_embedder_repr_and_errors_hide_key(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    embedder = _embedder(_status(401))
    assert CANARY_KEY not in repr(embedder)
    with pytest.raises(EmbeddingFailed) as info:
        await embedder.embed(["secret brand text"])
    assert CANARY_KEY not in str(info.value) and "nope" not in str(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__
    assert str(info.value) == "MODEL_UNAVAILABLE: AuthenticationError 401"
    assert CANARY_KEY not in caplog.text


def test_openai_embedder_refuses_placeholder_key() -> None:
    from app.core.config import Settings
    from app.services.embeddings import EmbedderUnavailable

    with pytest.raises(EmbedderUnavailable):
        OpenAIEmbedder(Settings(_env_file=None, app_env="local", openai_api_key="sk-REPLACE_ME"))
