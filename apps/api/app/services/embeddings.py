"""Embedding calls (CANON.md:126, :129: the backend owns them; S14b CD1, S16 CP1/CP2).

One of the two modules allowed to call a model (ruff TID251 exempts it). S14b builds the shape and
the local stand-in; S16 adds `OpenAIEmbedder`, the key branch in `get_embedder`, `EmbeddingFailed`
and metering, without renaming anything here.

- `Embedder`: the protocol every embedder implements (`model`, `async embed(texts)`).
- `HashEmbedder(model)`: signed feature hashing (S16 CP2). Each casefolded word token gives 8
  `(index, sign)` pairs from `sha256(model|token)`; the pairs are summed over the text and the
  vector is unit-normalised (1536-d). A text with no word token falls back to S9's
  `hash_vector(text)`. Stdlib only, deterministic, no I/O; texts that share words score higher.
- `get_embedder(settings)`: `HashEmbedder(settings.embedding_model)` under `APP_ENV=local`, else
  `EmbedderUnavailable` (`MODEL_UNAVAILABLE`) until S16 adds the real embedder.
"""

from __future__ import annotations

import hashlib
import math
import re
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from mkos_schemas.codes import ErrorCode

from app.core.config import EMBEDDING_DIMENSIONS, Settings
from app.core.vectors import hash_vector

PAIRS_PER_TOKEN: Final = 8
_WORD: Final = re.compile(r"\w+")


@dataclass(frozen=True)
class EmbeddingBatch:
    vectors: list[list[float]]
    input_tokens: int
    model: str


class Embedder(Protocol):
    model: str

    async def embed(self, texts: Sequence[str]) -> EmbeddingBatch: ...


class EmbedderUnavailable(Exception):  # noqa: N818 - the S16 CODE-PLAN name (CD1)
    """No embedder is configured for this environment. The message names no value."""

    code: Final = ErrorCode.MODEL_UNAVAILABLE

    def __init__(self) -> None:
        super().__init__("no embedder is configured for this APP_ENV")


def _tokens(text: str) -> list[str]:
    return [t.casefold() for t in _WORD.findall(text)]


class HashEmbedder:
    """The deterministic local embedder (see the module docstring)."""

    def __init__(self, model: str) -> None:
        if not model:
            raise ValueError("model must not be empty")
        self.model = model

    def _vector(self, text: str) -> tuple[list[float], int]:
        tokens = _tokens(text)
        if not tokens:
            return hash_vector(text or " "), 1
        acc = [0.0] * EMBEDDING_DIMENSIONS
        prefix = self.model.encode("utf-8") + b"|"
        for token in tokens:
            digest = hashlib.sha256(prefix + token.encode("utf-8")).digest()
            for (word,) in struct.iter_unpack(">I", digest[: 4 * PAIRS_PER_TOKEN]):
                index = word % EMBEDDING_DIMENSIONS
                sign = 1.0 if (word // EMBEDDING_DIMENSIONS) % 2 == 0 else -1.0
                acc[index] += sign
        norm = math.sqrt(math.fsum(v * v for v in acc))
        if norm == 0.0:  # every pair cancelled out: still a usable, deterministic vector
            return hash_vector(text), len(tokens)
        return [v / norm for v in acc], len(tokens)

    async def embed(self, texts: Sequence[str]) -> EmbeddingBatch:
        vectors: list[list[float]] = []
        tokens = 0
        for text in texts:
            vector, count = self._vector(text)
            vectors.append(vector)
            tokens += count
        return EmbeddingBatch(vectors=vectors, input_tokens=max(tokens, 1), model=self.model)


def get_embedder(settings: Settings) -> Embedder:
    """The one embedder selector (S16 CP1). S16 inserts the `OpenAIEmbedder` key branch first."""
    if settings.app_env == "local":
        return HashEmbedder(settings.embedding_model)
    raise EmbedderUnavailable
