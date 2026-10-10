"""Embedding calls (CANON.md:126, :129: the backend owns them; S14b CD1, S16 CP1/CP2/CP8/CP12).

One of the two modules allowed to call a model (ruff TID251 exempts it). S14b built the shape and
the local stand-in; S16 adds `OpenAIEmbedder`, the key branch in `get_embedder`, `EmbeddingFailed`
and metering, without renaming anything here.

- `Embedder`: the protocol every embedder implements (`model`, `async embed(texts)`).
- `HashEmbedder(model)`: signed feature hashing (S16 CP2). Each casefolded word token gives 8
  `(index, sign)` pairs from `sha256(model|token)`; the pairs are summed over the text and the
  vector is unit-normalised (1536-d). A text with no word token falls back to S9's
  `hash_vector(text)`. Stdlib only, deterministic, no I/O; texts that share words score higher.
- `OpenAIEmbedder(settings)`: `embeddings.create` with `max_retries=0` (CP8: one HTTP attempt is
  one call and one `agent_runs` row; the job's attempt cap is the only retry loop) and the
  `LLM_TIMEOUT_SECONDS` timeout. Failures become `EmbeddingFailed` (`TIMEOUT` retryable; 429, 5xx
  and connection errors `MODEL_UNAVAILABLE` retryable; other 4xx `MODEL_UNAVAILABLE` not), whose
  message is the class name and HTTP status only: never the key, the input or the provider text.
  A 200 reply of an unusable shape (wrong types, non-finite numbers, missing `usage`, indexes not
  exactly 0..n-1) is `MODEL_UNAVAILABLE` not retryable, so it is still metered (audit JOBS-5).
- `get_embedder(settings)`: a usable `OPENAI_API_KEY` → `OpenAIEmbedder`; else `APP_ENV=local` →
  `HashEmbedder(settings.embedding_model)`; else `EmbedderUnavailable` (`MODEL_UNAVAILABLE`).
- `embed_texts` / `embed_query`: the only callers of `Embedder.embed` (invariant 7). Each call of
  at most `MAX_INPUTS_PER_CALL` inputs writes exactly one `agent_runs` row, success or failure,
  with a sha256 digest of the inputs and never their text.
"""

from __future__ import annotations

import hashlib
import math
import re
import struct
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Protocol
from uuid import UUID, uuid4

import httpx2
import openai
from mkos_schemas.agent import AgentRunRecord, TokenUsage
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource
from mkos_schemas.errors import ErrorItem, ErrorReport
from openai import AsyncOpenAI
from openai.types import CreateEmbeddingResponse

from app.core.config import EMBEDDING_DIMENSIONS, Settings
from app.core.model_prices import embedding_cost_usd, is_usable_openai_key
from app.core.vectors import hash_vector
from app.services.telemetry import record_agent_run

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
    """The one embedder selector (S16 CP1): a usable key → OpenAI; else local → hash vectors."""
    if is_usable_openai_key(settings.openai_api_key):
        return OpenAIEmbedder(settings)
    if settings.app_env == "local":
        return HashEmbedder(settings.embedding_model)
    raise EmbedderUnavailable


# ---- OpenAI (S16 T6; CP8) ----


class EmbeddingFailed(Exception):  # noqa: N818 - the S16 CODE-PLAN name
    """One failed embedding call. `code` is `TIMEOUT` or `MODEL_UNAVAILABLE`; the message holds
    only the exception class and the HTTP status, never the key, the input or provider text."""

    def __init__(
        self,
        code: ErrorCode,
        *,
        retryable: bool,
        timed_out: bool = False,
        cause: str = "",
        status: int | None = None,
    ) -> None:
        detail = f"{cause} {status}".strip() if status is not None else cause
        super().__init__(f"{code.value}: {detail}" if detail else code.value)
        self.code = code
        self.retryable = retryable
        self.timed_out = timed_out
        self.status = status


OPENAI_BASE_URL: Final = "https://api.openai.com/v1"


class OpenAIEmbedder:
    """`text-embedding-3-small` (or the configured model) at 1536 dimensions."""

    def __init__(
        self, settings: Settings, *, http_client: httpx2.AsyncClient | None = None
    ) -> None:
        key = settings.openai_api_key
        if not is_usable_openai_key(key):
            raise EmbedderUnavailable
        self.model = settings.embedding_model
        # base_url is pinned so an OPENAI_BASE_URL in the environment can never redirect brand
        # text; max_retries=0 so the SDK never hides a call (CP8).
        self._client = AsyncOpenAI(
            api_key=key,
            base_url=OPENAI_BASE_URL,
            timeout=float(settings.llm_timeout_seconds),
            max_retries=0,
            http_client=http_client,
        )

    def __repr__(self) -> str:
        return f"OpenAIEmbedder(model={self.model!r})"

    async def embed(self, texts: Sequence[str]) -> EmbeddingBatch:
        try:
            response = await self._client.embeddings.create(
                model=self.model, input=list(texts), encoding_format="float"
            )
        except openai.APITimeoutError:
            raise EmbeddingFailed(
                ErrorCode.TIMEOUT, retryable=True, timed_out=True, cause="APITimeoutError"
            ) from None
        except openai.APIConnectionError:
            raise EmbeddingFailed(
                ErrorCode.MODEL_UNAVAILABLE, retryable=True, cause="APIConnectionError"
            ) from None
        except openai.APIStatusError as exc:
            status = int(exc.status_code)
            raise EmbeddingFailed(
                ErrorCode.MODEL_UNAVAILABLE,
                retryable=status == 429 or status >= 500,
                cause=type(exc).__name__,
                status=status,
            ) from None
        except openai.OpenAIError as exc:  # a malformed response body, never retried
            raise EmbeddingFailed(
                ErrorCode.MODEL_UNAVAILABLE, retryable=False, cause=type(exc).__name__
            ) from None
        if not isinstance(response, CreateEmbeddingResponse):  # a 200 with a non-JSON body
            raise _unexpected_shape()
        return EmbeddingBatch(
            vectors=_vectors(response, len(texts)),
            input_tokens=_prompt_tokens(response),
            model=self.model,
        )


def _unexpected_shape() -> EmbeddingFailed:
    return EmbeddingFailed(
        ErrorCode.MODEL_UNAVAILABLE, retryable=False, cause="unexpected embedding shape"
    )


def _vectors(response: CreateEmbeddingResponse, count: int) -> list[list[float]]:
    """One finite 1536-d vector per input, ordered by `index`, which must be exactly 0..count-1.
    The SDK builds the reply without validating it, so every type is checked here (audit
    JOBS-5): anything else is `EmbeddingFailed`, never a raw TypeError/ValueError."""
    data = getattr(response, "data", None)
    if not isinstance(data, list) or len(data) != count:
        raise _unexpected_shape()
    by_index: dict[int, list[float]] = {}
    for item in data:
        index = getattr(item, "index", None)
        embedding = getattr(item, "embedding", None)
        if type(index) is not int or index in by_index:
            raise _unexpected_shape()
        if not isinstance(embedding, list) or len(embedding) != EMBEDDING_DIMENSIONS:
            raise _unexpected_shape()
        vector: list[float] = []
        for value in embedding:
            if type(value) not in (int, float) or not math.isfinite(value):
                raise _unexpected_shape()
            vector.append(float(value))
        by_index[index] = vector
    if sorted(by_index) != list(range(count)):
        raise _unexpected_shape()
    return [by_index[i] for i in range(count)]


def _prompt_tokens(response: CreateEmbeddingResponse) -> int:
    tokens = getattr(getattr(response, "usage", None), "prompt_tokens", None)
    if type(tokens) is not int or tokens < 0:
        raise _unexpected_shape()
    return tokens


# ---- metered calls (S16 T8; invariant 7) ----

MAX_INPUTS_PER_CALL: Final = 100  # R12269
QUERY_MAX_CHARS: Final = 2000


def input_digest(texts: Sequence[str]) -> str:
    return hashlib.sha256("\x1f".join(texts).encode("utf-8")).hexdigest()


def _failure_report(exc: EmbeddingFailed, attempt: int, request_id: str) -> ErrorReport:
    message = "the embedding call timed out" if exc.timed_out else "the embedding call failed"
    item = ErrorItem(
        field="request",
        error_code=exc.code,
        message=message,
        source=ErrorSource.AI_MODEL,
        retryable=exc.retryable,
    )
    return ErrorReport(
        status="failed",
        error_code=exc.code,
        retryable=exc.retryable,
        attempts_used=attempt,
        max_attempts=3,
        errors=[item],
        request_id=request_id,
    )


async def _metered_call(
    embedder: Embedder,
    texts: Sequence[str],
    *,
    tenant_id: UUID,
    job_id: UUID | None,
    attempt: int,
    request_id: str,
) -> EmbeddingBatch:
    started_at = datetime.now(UTC)
    start = time.monotonic()
    common: dict[str, Any] = {
        "id": uuid4(),
        "tenant_id": tenant_id,
        "request_id": request_id,
        "job_id": job_id,
        "agent_name": "embedding",
        "trigger": "embed",
        "attempt": attempt,
        "model_used": embedder.model,
        "input_digest": input_digest(texts),
        "started_at": started_at,
        "created_at": started_at,
    }
    try:
        batch = await embedder.embed(texts)
    except EmbeddingFailed as exc:
        await record_agent_run(
            AgentRunRecord(
                **common,
                duration_ms=_elapsed_ms(start),
                status="timeout" if exc.timed_out else "failed",
                error_report=_failure_report(exc, attempt, request_id),
            )
        )
        raise
    await record_agent_run(
        AgentRunRecord(
            **common,
            tokens=TokenUsage(input=batch.input_tokens),
            cost_usd=embedding_cost_usd(embedder.model, batch.input_tokens),
            duration_ms=_elapsed_ms(start),
            status="completed",
        )
    )
    return batch


def _elapsed_ms(start: float) -> int:
    return max(0, int((time.monotonic() - start) * 1000))


async def embed_texts(
    embedder: Embedder,
    texts: Sequence[str],
    *,
    tenant_id: UUID,
    job_id: UUID | None,
    attempt: int,
    request_id: str,
) -> list[EmbeddingBatch]:
    """One metered call per at most `MAX_INPUTS_PER_CALL` texts, in order; no call for none."""
    batches: list[EmbeddingBatch] = []
    for start in range(0, len(texts), MAX_INPUTS_PER_CALL):
        batches.append(
            await _metered_call(
                embedder,
                texts[start : start + MAX_INPUTS_PER_CALL],
                tenant_id=tenant_id,
                job_id=job_id,
                attempt=attempt,
                request_id=request_id,
            )
        )
    return batches


async def embed_query(
    embedder: Embedder,
    text: str,
    *,
    tenant_id: UUID,
    job_id: UUID | None,
    attempt: int,
    request_id: str,
) -> list[float]:
    """The query vector of one retrieval (S14b's `retrieve_brand_chunks`), metered like any call.
    A blank or over-long query raises `ValueError` before any call or row."""
    if not isinstance(text, str) or not text.strip() or len(text) > QUERY_MAX_CHARS:
        raise ValueError(f"query must be 1..{QUERY_MAX_CHARS} non-blank characters")
    batch = await _metered_call(
        embedder, [text], tenant_id=tenant_id, job_id=job_id, attempt=attempt, request_id=request_id
    )
    return batch.vectors[0]
