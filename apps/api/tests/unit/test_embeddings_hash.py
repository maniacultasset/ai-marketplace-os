"""S14b T4 (AC 3; D4, CD1): the shared embedder module, with S16 CODE-PLAN's names (S16 appends its
key branch and its own tests to this file)."""

from __future__ import annotations

import ast
import math
from pathlib import Path
from typing import Any

import pytest

MODULE = Path(__file__).resolve().parents[2] / "app" / "services" / "embeddings.py"


def _settings(app_env: str = "local", **over: Any) -> Any:
    from app.core.config import Settings

    return Settings(_env_file=None, app_env=app_env, **over)


def _cos(a: list[float], b: list[float]) -> float:
    return math.fsum(x * y for x, y in zip(a, b, strict=True))


async def test_hash_embedder_deterministic() -> None:
    from app.services.embeddings import HashEmbedder

    one = await HashEmbedder("m").embed(["cotton kurta", "brass lamp"])
    two = await HashEmbedder("m").embed(["cotton kurta", "brass lamp"])
    assert one == two
    other_model = await HashEmbedder("n").embed(["cotton kurta"])
    assert other_model.vectors[0] != one.vectors[0]


async def test_hash_embedder_length_1536() -> None:
    from app.services.embeddings import HashEmbedder

    batch = await HashEmbedder("m").embed(["a b c", "!!!"])
    assert [len(v) for v in batch.vectors] == [1536, 1536]


@pytest.mark.parametrize("text", ["cotton kurta set", "x", "Devanagari कुर्ता 🎉", "?!", " "])
async def test_hash_embedder_unit_norm(text: str) -> None:
    from app.services.embeddings import HashEmbedder

    (vector,) = (await HashEmbedder("m").embed([text])).vectors
    assert math.isclose(math.fsum(v * v for v in vector), 1.0, rel_tol=1e-9)


async def test_hash_embedder_no_word_tokens_fallback_nonzero() -> None:
    from app.core.vectors import hash_vector
    from app.services.embeddings import HashEmbedder

    (vector,) = (await HashEmbedder("m").embed(["?!"])).vectors
    assert any(v != 0.0 for v in vector)
    assert vector == hash_vector("?!")


async def test_hash_embedder_model_name_is_settings_model() -> None:
    from app.services.embeddings import get_embedder

    embedder = get_embedder(_settings(embedding_model="text-embedding-3-small"))
    assert embedder.model == "text-embedding-3-small"
    assert (await embedder.embed(["x"])).model == "text-embedding-3-small"


async def test_hash_embedder_tokens_counted() -> None:
    from app.services.embeddings import HashEmbedder

    assert (await HashEmbedder("m").embed(["one two three", "four"])).input_tokens == 4
    assert (await HashEmbedder("m").embed(["?!"])).input_tokens == 1
    assert (await HashEmbedder("m").embed([])).input_tokens == 1


async def test_hash_embedder_shared_words_rank_first() -> None:
    from app.services.embeddings import HashEmbedder

    corpus = [
        "Lead with fabric and fit.",
        "Never claim best or guaranteed.",
        "Name the material first.",
        "Keep titles under 150 characters.",
        "Brass table lamp with a linen shade.",
    ]
    embedder = HashEmbedder("m")
    docs = (await embedder.embed(corpus)).vectors
    (query,) = (await embedder.embed(["brass lamp shade"])).vectors
    ranked = sorted(range(len(corpus)), key=lambda i: -_cos(query, docs[i]))
    assert ranked[0] == 4


async def test_embedder_selection_no_key_local_fake() -> None:
    from app.services.embeddings import HashEmbedder, get_embedder

    assert isinstance(get_embedder(_settings("local")), HashEmbedder)


@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_embedder_selection_no_key_staging_raises(app_env: str) -> None:
    from app.services.embeddings import EmbedderUnavailable, get_embedder
    from mkos_schemas.codes import ErrorCode

    with pytest.raises(EmbedderUnavailable) as caught:
        get_embedder(_settings(app_env, sentry_dsn="https://p@o0.ingest.sentry.io/0"))
    assert caught.value.code is ErrorCode.MODEL_UNAVAILABLE


def test_embeddings_imports_no_network() -> None:
    banned = {"socket", "httpx", "openai", "requests", "urllib", "aiohttp", "http"}
    found: set[str] = set()
    for node in ast.walk(ast.parse(MODULE.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
    assert found & banned == set()
