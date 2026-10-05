"""`BrandChunkRepository.search`: the tenant's nearest brand chunks (S9 T9; CANON.md:137).

Year-1 search is exact, filter-first (DATABASE.md:1068-1075): a `MATERIALIZED` CTE narrows to
the tenant's active chunks of the current embedding model (and the asked types) before any
distance is computed, so the HNSW index never sees another tenant's rows. Every input is
validated before any SQL runs.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Final, get_args

from mkos_schemas.jobs import BrandChunkDTO, BrandChunkType
from sqlalchemy import select
from sqlalchemy.orm import aliased

from app.core.config import EMBEDDING_DIMENSIONS, get_settings
from app.db.models.audit_brand import BrandChunk
from app.repositories.base import TenantScopedRepository

K_MIN: Final = 1
K_MAX: Final = 50
CHUNK_TYPES: Final[frozenset[str]] = frozenset(get_args(BrandChunkType))


def _validate(
    query_embedding: Sequence[float], k: int, types: Sequence[str] | None
) -> tuple[list[float], list[str] | None]:
    if isinstance(k, bool) or not isinstance(k, int) or not K_MIN <= k <= K_MAX:
        raise ValueError(f"k must be an int in {K_MIN}..{K_MAX}")
    if isinstance(query_embedding, (str, bytes)) or len(query_embedding) != EMBEDDING_DIMENSIONS:
        raise ValueError(f"query_embedding must hold {EMBEDDING_DIMENSIONS} floats")
    vector = [float(v) for v in query_embedding]
    if not all(math.isfinite(v) for v in vector):
        raise ValueError("query_embedding must be finite")
    if math.fsum(v * v for v in vector) <= 0.0:
        raise ValueError("query_embedding must not be the zero vector")
    if types is None:
        return vector, None
    if isinstance(types, str) or len(types) == 0:
        raise ValueError("types must be None or a non-empty sequence of chunk types")
    unknown = sorted({str(t) for t in types} - CHUNK_TYPES)
    if unknown or not all(isinstance(t, str) for t in types):
        raise ValueError(f"unknown chunk type(s) {unknown}")
    return vector, sorted(set(types))


class BrandChunkRepository(TenantScopedRepository[BrandChunk]):
    model = BrandChunk

    async def search(
        self,
        query_embedding: Sequence[float],
        k: int = 8,
        types: Sequence[BrandChunkType] | None = None,
    ) -> list[BrandChunkDTO]:
        vector, wanted = _validate(query_embedding, k, types)
        inner = self._select().where(
            BrandChunk.is_active.is_(True),
            BrandChunk.embedding_model == get_settings().embedding_model,
        )
        if wanted is not None:
            inner = inner.where(BrandChunk.type.in_(wanted))
        candidates = inner.cte("candidates").prefix_with("MATERIALIZED", dialect="postgresql")
        chunk = aliased(BrandChunk, candidates)
        distance = chunk.embedding.cosine_distance(vector).label("distance")
        stmt = self._scoped_outer(
            select(chunk, distance).order_by(distance, chunk.id).limit(k), candidates
        )
        rows = (await self.db.execute(stmt)).all()
        return [
            BrandChunkDTO(
                id=row[0].id,
                tenant_id=row[0].tenant_id,
                type=row[0].type,
                text=row[0].text,
                similarity=min(1.0, max(-1.0, 1.0 - float(row[1]))),
                source_type=row[0].source_type,
                source_id=row[0].source_id,
                chunk_index=row[0].chunk_index,
            )
            for row in rows
        ]
