"""Audit JOBS-5: a 200 embeddings reply that parses but carries an unusable shape is
`EmbeddingFailed(MODEL_UNAVAILABLE, retryable=False)`, never a raw TypeError/ValueError/
AttributeError, so the metered call still writes its `agent_runs` row (invariant 7)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx2
import pytest
from app.services.embeddings import EmbeddingFailed
from mkos_schemas.codes import ErrorCode
from tests.unit.test_embeddings_openai import DIMS, Script, _embedder, _ok


def _with(mutate: Callable[[dict[str, Any]], Any], n: int = 2) -> Script:
    body = _ok(["t"] * n)
    mutate(body)
    return Script(lambda r: httpx2.Response(200, json=body))


def _set(path: tuple[Any, ...], value: Any) -> Callable[[dict[str, Any]], None]:
    def mutate(body: dict[str, Any]) -> None:
        target: Any = body
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    return mutate


CASES: dict[str, Callable[[dict[str, Any]], Any]] = {
    "null-element": lambda b: b["data"][0]["embedding"].__setitem__(5, None),
    "string-element": lambda b: b["data"][0]["embedding"].__setitem__(5, "0.1"),
    "list-element": lambda b: b["data"][0]["embedding"].__setitem__(5, [0.1]),
    "embedding-string": _set(("data", 0, "embedding"), "x" * DIMS),
    "embedding-null": _set(("data", 0, "embedding"), None),
    "no-usage": lambda b: b.pop("usage"),
    "usage-null": _set(("usage",), None),
    "tokens-string": _set(("usage", "prompt_tokens"), "seven"),
    "tokens-negative": _set(("usage", "prompt_tokens"), -1),
    "tokens-float": _set(("usage", "prompt_tokens"), 7.5),
    "no-index": lambda b: b["data"][0].pop("index"),
    "index-string": _set(("data", 0, "index"), "0"),
    "index-duplicate": _set(("data", 1, "index"), 0),
    "index-gap": _set(("data", 1, "index"), 5),
    "data-null": _set(("data",), None),
    "data-object": _set(("data",), {"0": []}),
    "item-not-object": _set(("data", 0), [0.0] * DIMS),
}


@pytest.mark.parametrize("name", list(CASES))
async def test_malformed_embedding_reply_is_embedding_failed(name: str) -> None:
    with pytest.raises(EmbeddingFailed) as raised:
        await _embedder(_with(CASES[name])).embed(["a", "b"])
    assert raised.value.code == ErrorCode.MODEL_UNAVAILABLE
    assert raised.value.retryable is False


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
async def test_non_finite_element_is_embedding_failed(value: str) -> None:
    """Python's JSON parser accepts these literals; a vector holding one is unusable."""
    body = _ok(["t"])
    raw = httpx2.Response(200, json=body).content.replace(b"[1.0,", f"[{value},".encode(), 1)
    assert raw != httpx2.Response(200, json=body).content
    script = Script(lambda r: httpx2.Response(200, content=raw))
    with pytest.raises(EmbeddingFailed):
        await _embedder(script).embed(["t"])


async def test_well_formed_reply_still_accepted() -> None:
    batch = await _embedder(Script(lambda r: httpx2.Response(200, json=_ok(["a", "b"])))).embed(
        ["a", "b"]
    )
    assert [v[0] for v in batch.vectors] == [1.0, 2.0]
    assert batch.input_tokens == 14
