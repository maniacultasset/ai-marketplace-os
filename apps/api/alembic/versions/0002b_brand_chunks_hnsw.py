"""brand_chunks HNSW and partial indexes, built CONCURRENTLY in autocommit

DATABASE.md §5.5 brand_chunks and §8.2 ledger row 0002b; CANON.md ledger row 0002b. First the
gate: pgvector >= 0.8.2 (the iterative-scan settings DATABASE.md relies on), refused with a clear
message otherwise; `--sql` has no connection, so the offline render skips it. Then, outside any
transaction (CREATE INDEX CONCURRENTLY cannot run inside one): the cosine HNSW index and the
`(tenant_id, type) WHERE is_active` index. Year-1 search is exact and filters first (T66); HNSW
serves a tenant past ~20k chunks.

A failed CONCURRENTLY build leaves an INVALID index; recovering from one on Cloud SQL closes with
the staging step (S4/S12), not here.

Revision ID: 0002b_brand_chunks_hnsw
Revises: 0002_listing_pipeline
Create Date: 2026-10-05

"""

from collections.abc import Sequence
from typing import Final

from alembic import context, op
from app.db.migrations import require_extension_version

# revision identifiers, used by Alembic.
revision: str = "0002b_brand_chunks_hnsw"
down_revision: str | Sequence[str] | None = "0002_listing_pipeline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PGVECTOR_MINIMUM: Final = (0, 8, 2)

UP: Final[tuple[str, ...]] = (
    """
    CREATE INDEX CONCURRENTLY brand_chunks_embedding_idx ON public.brand_chunks
      USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)
    """,
    """
    CREATE INDEX CONCURRENTLY brand_chunks_tenant_type_idx ON public.brand_chunks (tenant_id, type)
      WHERE is_active
    """,
)
DOWN: Final[tuple[str, ...]] = (
    "DROP INDEX CONCURRENTLY public.brand_chunks_tenant_type_idx",
    "DROP INDEX CONCURRENTLY public.brand_chunks_embedding_idx",
)


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    if not context.is_offline_mode():
        require_extension_version(op.get_bind(), "vector", PGVECTOR_MINIMUM)
    with op.get_context().autocommit_block():
        for statement in UP:
            op.execute(statement)


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    with op.get_context().autocommit_block():
        for statement in DOWN:
            op.execute(statement)
