"""candidate_cache_accepted: accepted image candidates leave the cache index (ADR-0030 amendment)

CANON.md ledger row 0030 (claimed 2026-11-11 under the V1.1 delegation; down_revision
`0029_admin_google_link`, the single head on `main` after #155). Expand only, as migrator, no
table change.

`uq_image_candidates_cache` held accepted rows, so an accepted candidate answered every later
create of the same pixels as "accepted" with no new job, even after its image was deleted or
replaced, or when the same photo became the main of another colourway. An accepted row is
provenance: it now stays outside the index, and the create service answers "already applied"
only while its image is still the live main of the asking product and colourway.

The index is rebuilt CONCURRENTLY in an autocommit block under a temporary name, the old one
dropped, and the new one renamed to `uq_image_candidates_cache` (the name the services and tests
read). The new predicate covers a subset of the old one's rows, so the build cannot fail on
existing data. Between this migration and the new API revision taking traffic, the old revision's
`ON CONFLICT` no longer infers the index and a candidate create there fails with a 500 (seconds,
during a deploy; the seller clicks again).

Downgrade (local and CI only, RULES.md "No prod downgrade") refuses while an accepted row shares
its cache key with another covered row, then restores the 0018 predicate the same way.

Revision ID: 0030_candidate_cache_accepted
Revises: 0028_photo_read_on_upload
Create Date: 2026-11-11

"""

from collections.abc import Sequence
from typing import Final

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0030_candidate_cache_accepted"
# The single head on main at build (0029_admin_google_link, PR #155, is not merged yet).
down_revision: str | Sequence[str] | None = "0029_admin_google_link"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX: Final = "uq_image_candidates_cache"
NEXT: Final = "uq_image_candidates_cache_next"
COLUMNS: Final = "public.image_candidates (tenant_id, product_id, kind, source_hash, generator_key)"
RETRYABLE: Final = (
    "NOT (status = 'failed' AND failure_reason IN "
    "('model_failed', 'credit_used_up', 'budget', 'source_changed'))"
)
WITHOUT_ACCEPTED: Final = f"status <> 'accepted' AND {RETRYABLE}"

# an accepted row and another covered row on one cache key: the 0018 index cannot hold them
REFUSE_DOWNGRADE: Final = f"""
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM public.image_candidates
    WHERE {RETRYABLE}
    GROUP BY tenant_id, product_id, kind, source_hash, generator_key
    HAVING count(*) > 1
  ) THEN
    RAISE EXCEPTION '0030 downgrade refused: an accepted image candidate shares its cache key'
      USING ERRCODE = 'GRD01';
  END IF;
END
$$
"""  # noqa: S608 - module constants only, no input


def _swap(predicate: str) -> None:
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s'")
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS public.{NEXT}")
        op.execute(f"CREATE UNIQUE INDEX CONCURRENTLY {NEXT} ON {COLUMNS} WHERE {predicate}")
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS public.{INDEX}")
        op.execute(f"ALTER INDEX public.{NEXT} RENAME TO {INDEX}")


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    _swap(WITHOUT_ACCEPTED)


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.execute(REFUSE_DOWNGRADE)
    _swap(RETRYABLE)
