"""`BrandChunkRepository.search`: the tenant's nearest brand chunks (S9 T9; CANON.md:137).

Year-1 search is exact, filter-first (DATABASE.md:1068-1075): a `MATERIALIZED` CTE narrows to
the tenant's active chunks of the current embedding model (and the asked types) before any
distance is computed, so the HNSW index never sees another tenant's rows. Every input is
validated before any SQL runs.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final, get_args

from mkos_schemas.jobs import BrandChunkDTO, BrandChunkType
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import aliased

from app.core.config import EMBEDDING_DIMENSIONS, get_settings
from app.db.guard import TENANT_INFO_KEY, CrossTenantQueryError
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


@dataclass(frozen=True, slots=True)
class NewChunk:
    """One chunk to insert (S16 T10). `token_count` is None for OpenAI (usage is per call)."""

    type: BrandChunkType
    text: str
    embedding: Sequence[float]
    embedding_model: str
    content_hash: str
    source_type: str
    source_id: uuid.UUID
    chunk_index: int
    token_count: int | None = None


class BrandChunkRepository(TenantScopedRepository[BrandChunk]):
    model = BrandChunk

    # ---- S16 writes (T10): every write names the tenant and the active-row index ----

    def _own_session(self) -> None:
        if self.db.info.get(TENANT_INFO_KEY) != self.tenant_id:
            raise CrossTenantQueryError("brand_chunks write for another tenant's unit of work")

    def _active(self, source_type: str, source_id: uuid.UUID) -> list[Any]:
        return [
            BrandChunk.source_type == source_type,
            BrandChunk.source_id == source_id,
            BrandChunk.is_active.is_(True),
        ]

    async def active_for_source(
        self, source_type: str, source_id: uuid.UUID, model: str
    ) -> dict[int, str]:
        stmt = (
            self._select()
            .where(*self._active(source_type, source_id), BrandChunk.embedding_model == model)
            .with_only_columns(BrandChunk.chunk_index, BrandChunk.content_hash)
        )
        return {int(i): str(h) for i, h in (await self.db.execute(stmt)).all()}

    async def vectors_by_hash(
        self, source_type: str, source_id: uuid.UUID, hashes: Sequence[str], model: str
    ) -> dict[str, list[float]]:
        """Stored vectors of this source (any active state) for `hashes`, model-scoped."""
        if not hashes:
            return {}
        stmt = (
            self._select()
            .where(
                BrandChunk.source_type == source_type,
                BrandChunk.source_id == source_id,
                BrandChunk.embedding_model == model,
                BrandChunk.content_hash.in_(sorted(set(hashes))),
            )
            .with_only_columns(BrandChunk.content_hash, BrandChunk.embedding)
        )
        return {str(h): [float(v) for v in e] for h, e in (await self.db.execute(stmt)).all()}

    async def insert_many(self, rows: Sequence[NewChunk]) -> None:
        """One INSERT for all rows; a row whose active slot is taken is skipped (a replay)."""
        if not rows:
            return
        self._own_session()
        values = [
            {
                "tenant_id": self.tenant_id,
                "type": r.type,
                "text": r.text,
                "embedding": list(r.embedding),
                "embedding_model": r.embedding_model,
                "content_hash": r.content_hash,
                "source_type": r.source_type,
                "source_id": r.source_id,
                "chunk_index": r.chunk_index,
                "token_count": r.token_count,
            }
            for r in rows
        ]
        stmt = (
            pg_insert(BrandChunk)
            .values(values)
            .on_conflict_do_nothing(
                index_elements=[
                    BrandChunk.tenant_id,
                    BrandChunk.source_type,
                    BrandChunk.source_id,
                    BrandChunk.chunk_index,
                    BrandChunk.embedding_model,
                ],
                # the index predicate verbatim (`is_active IS true` does not prove it)
                index_where=text("is_active"),
            )
        )
        await self.db.execute(stmt)

    async def _deactivate(self, where: list[Any]) -> int:
        self._own_session()
        stmt = self._scoped_write(
            update(BrandChunk)
            .where(*where, self._tenant_criterion(include_platform=False))
            .values(is_active=False)
        )
        result = await self.db.execute(stmt)
        return int(getattr(result, "rowcount", 0) or 0)

    async def deactivate_source(
        self, source_type: str, source_id: uuid.UUID, *, indexes: Sequence[int] | None = None
    ) -> int:
        where = self._active(source_type, source_id)
        if indexes is not None:
            if not indexes:
                return 0
            where.append(BrandChunk.chunk_index.in_(sorted(set(indexes))))
        return await self._deactivate(where)

    async def count_active(self, source_type: str, source_id: uuid.UUID, model: str) -> int:
        stmt = (
            self._select()
            .where(*self._active(source_type, source_id), BrandChunk.embedding_model == model)
            .with_only_columns(func.count())
        )
        return int((await self.db.execute(stmt)).scalar_one())

    async def count_active_of_type(self, source_type: str) -> int:
        stmt = (
            self._select()
            .where(BrandChunk.source_type == source_type, BrandChunk.is_active.is_(True))
            .with_only_columns(func.count())
        )
        return int((await self.db.execute(stmt)).scalar_one())

    async def active_profile_set(self, model: str) -> list[tuple[uuid.UUID, int, str]]:
        """CP11: every active `brand_profile` row of the tenant, whatever its `source_id`
        (the seed's rows included), `source_id = tenant_id` first, then by index."""
        stmt = (
            self._select()
            .where(
                BrandChunk.source_type == "brand_profile",
                BrandChunk.is_active.is_(True),
                BrandChunk.embedding_model == model,
            )
            .with_only_columns(
                BrandChunk.source_id, BrandChunk.chunk_index, BrandChunk.content_hash
            )
            .order_by((BrandChunk.source_id != self.tenant_id), BrandChunk.chunk_index)
        )
        return [(sid, int(i), str(h)) for sid, i, h in (await self.db.execute(stmt)).all()]

    async def deactivate_profile_all(self) -> int:
        return await self._deactivate(
            [BrandChunk.source_type == "brand_profile", BrandChunk.is_active.is_(True)]
        )

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
