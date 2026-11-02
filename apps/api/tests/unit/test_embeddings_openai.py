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


# ---- ADR-0024: the same embedder through LLM_GATEWAY=openrouter ----

ROUTER_KEY = "canary-" + "router-key-" + "1a2b3c"


def _router_embedder(script: Script, **over: Any) -> OpenAIEmbedder:
    from app.core.config import Settings

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(script))
    cfg = Settings(
        _env_file=None,
        app_env="local",
        llm_gateway="openrouter",
        openai_api_key=CANARY_KEY,
        openrouter_api_key=ROUTER_KEY,
        **over,
    )
    return OpenAIEmbedder(cfg, http_client=client)


async def test_openrouter_embedder_request_shape() -> None:
    """The pinned OpenRouter base, the `openai/` model id and the router key; the OpenAI key is
    never sent. The batch keeps the OpenAI id, which brand chunks and prices are keyed by."""
    script = Script(lambda r: httpx2.Response(200, json=_ok(json.loads(r.content)["input"])))
    batch = await _router_embedder(script).embed(["one", "two"])
    (request,) = script.requests
    assert (request.url.scheme, request.url.host) == ("https", "openrouter.ai")
    assert request.url.path == "/api/v1/embeddings"
    body = json.loads(request.content)
    assert body["model"] == "openai/text-embedding-3-small"
    assert body["input"] == ["one", "two"] and body["encoding_format"] == "float"
    assert "dimensions" not in body
    assert request.headers["Authorization"] == f"Bearer {ROUTER_KEY}"
    assert CANARY_KEY not in str(request.headers)
    assert batch.model == "text-embedding-3-small" and batch.input_tokens == 14
    assert all(len(v) == DIMS for v in batch.vectors)


def test_default_http_client_ignores_proxy_env_and_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an injected client the key's client never reads HTTPS_PROXY/ALL_PROXY or
    SSL_CERT_FILE from the environment and never follows a redirect, in either gateway."""
    from app.core.config import Settings

    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.invalid:3128")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.example.invalid:3128")
    for cfg in (
        _settings(),
        Settings(
            _env_file=None, app_env="local", llm_gateway="openrouter", openrouter_api_key=ROUTER_KEY
        ),
    ):
        client = OpenAIEmbedder(cfg)._client._client
        assert client.trust_env is False and client.follow_redirects is False
        assert client.timeout.read == float(cfg.llm_timeout_seconds)


async def test_openrouter_embedder_base_url_env_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://evil.example.invalid/v1")
    script = Script(lambda r: httpx2.Response(200, json=_ok(["x"])))
    await _router_embedder(script).embed(["x"])
    assert script.requests[0].url.host == "openrouter.ai"


async def test_openrouter_embedder_no_sdk_retry_and_hides_key() -> None:
    script = Script(lambda _: httpx2.Response(500, json={"error": {"message": ROUTER_KEY}}))
    embedder = _router_embedder(script)
    with pytest.raises(EmbeddingFailed) as info:
        await embedder.embed(["a"])
    assert len(script.requests) == 1 and info.value.retryable
    assert ROUTER_KEY not in str(info.value) and ROUTER_KEY not in repr(embedder)


async def test_openrouter_embedding_cost_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    """One metered call writes one completed `agent_runs` row with the usage tokens and the
    $0.02/M price (14 tokens round to 0.000000; 1M tokens cost 0.02)."""
    import uuid
    from decimal import Decimal

    from app.services import embeddings

    rows: list[Any] = []

    async def capture(record: Any) -> None:
        rows.append(record)

    monkeypatch.setattr(embeddings, "record_agent_run", capture)

    def reply(request: httpx2.Request) -> httpx2.Response:
        # OpenRouter's real reply shape: its own id and provider, and cost fields in `usage`
        body = _ok(json.loads(request.content)["input"])
        body |= {"id": "gen-emb-1", "provider": "OpenAI"}
        body["usage"] = {
            "prompt_tokens": 1_000_000,
            "total_tokens": 1_000_000,
            "cost": 0.02,
            "is_byok": False,
            "cost_details": {"upstream_inference_cost": None},
        }
        return httpx2.Response(200, json=body)

    embedder = _router_embedder(Script(reply))
    await embeddings.embed_texts(
        embedder, ["a"], tenant_id=uuid.uuid4(), job_id=None, attempt=1, request_id="req-1"
    )
    (row,) = rows
    assert row.status == "completed" and row.model_used == "text-embedding-3-small"
    assert row.tokens.input == 1_000_000 and row.cost_usd == Decimal("0.020000")


@pytest.mark.parametrize(
    ("error", "retryable", "status"),
    [
        ({"code": 502, "message": "upstream down"}, True, 502),
        ({"code": 429, "message": "rate limited"}, True, 429),
        ({"message": "no code"}, True, None),
        ({"code": "server_error", "message": "string code"}, True, None),
        ({"code": 400, "message": "bad input"}, False, 400),
    ],
)
async def test_openrouter_error_body_on_200(
    error: dict[str, Any], retryable: bool, status: int | None
) -> None:
    """OpenRouter answers an upstream failure with a 200 and an `error` object: retried like the
    same status on the wire (a 400 is not), never as an unusable shape, and the provider's text
    stays out of the error."""
    body = {"error": {**error, "message": f"{error['message']} {ROUTER_KEY}"}}
    script = Script(lambda _: httpx2.Response(200, json=body))
    with pytest.raises(EmbeddingFailed) as info:
        await _router_embedder(script).embed(["a"])
    assert len(script.requests) == 1
    assert info.value.code is ErrorCode.MODEL_UNAVAILABLE
    assert (info.value.retryable, info.value.status) == (retryable, status)
    assert "unexpected embedding shape" not in str(info.value)
    assert ROUTER_KEY not in str(info.value) and error["message"] not in str(info.value)


@pytest.mark.parametrize("app_env", ["local", "staging"])
def test_get_embedder_under_openrouter_takes_the_router_key(app_env: str) -> None:
    from app.core.config import Settings
    from app.services.embeddings import get_embedder

    cfg = Settings(
        _env_file=None, app_env=app_env, llm_gateway="openrouter", openrouter_api_key=ROUTER_KEY
    )
    assert isinstance(get_embedder(cfg), OpenAIEmbedder)


def test_get_embedder_under_openrouter_ignores_the_openai_key() -> None:
    """The OpenAI key never stands in for the gateway's: local falls back to hash vectors,
    a deployed env has no embedder."""
    from app.core.config import Settings
    from app.services.embeddings import EmbedderUnavailable, HashEmbedder, get_embedder

    def cfg(app_env: str) -> Settings:
        return Settings(
            _env_file=None, app_env=app_env, llm_gateway="openrouter", openai_api_key=CANARY_KEY
        )

    assert isinstance(get_embedder(cfg("local")), HashEmbedder)
    with pytest.raises(EmbedderUnavailable):
        get_embedder(cfg("staging"))
