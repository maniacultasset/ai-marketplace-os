"""S14b T4 (AC 3; D4, CD1): the shared embedder module, with S16 CODE-PLAN's names (S16 appends its
key branch and its own tests to this file)."""

from __future__ import annotations

import math
from typing import Any

import pytest


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


async def test_hash_embedder_path_no_network() -> None:
    """S16 T6 narrows S14b's import ban: the module now imports `openai`/`httpx2` for the real
    embedder (TID251 keeps them out of the rest of `app/`), but the local no-key path makes no
    connection: pytest-socket blocks every socket in this suite."""
    from app.services.embeddings import HashEmbedder, get_embedder

    embedder = get_embedder(_settings("local"))
    assert isinstance(embedder, HashEmbedder)
    batch = await embedder.embed(["a brand sentence", "another one"])
    assert len(batch.vectors) == 2


def test_embedder_selection_placeholder_is_fake() -> None:
    from app.services.embeddings import HashEmbedder, get_embedder

    for key in ("sk-REPLACE_ME", "", "   "):
        assert isinstance(get_embedder(_settings("local", openai_api_key=key)), HashEmbedder)


@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_embedder_selection_placeholder_staging_raises(app_env: str) -> None:
    from app.services.embeddings import EmbedderUnavailable, get_embedder

    with pytest.raises(EmbedderUnavailable):
        get_embedder(_settings(app_env, openai_api_key="sk-REPLACE_ME"))


@pytest.mark.parametrize("app_env", ["local", "staging"])
def test_embedder_selection_real_key_openai(app_env: str) -> None:
    from app.services.embeddings import OpenAIEmbedder, get_embedder

    embedder = get_embedder(_settings(app_env, openai_api_key="test-only-not-a-real-key"))
    assert isinstance(embedder, OpenAIEmbedder)
    assert embedder.model == "text-embedding-3-small"


def test_hash_embedder_unit_norm_property() -> None:
    from app.services.embeddings import HashEmbedder
    from hypothesis import given, settings
    from hypothesis import strategies as st

    embedder = HashEmbedder("text-embedding-3-small")

    @settings(max_examples=100, deadline=None)
    @given(st.text(max_size=300))
    def check(text: str) -> None:
        vector, _ = embedder._vector(text)
        assert len(vector) == 1536
        assert abs(math.fsum(v * v for v in vector) - 1.0) < 1e-9

    check()
