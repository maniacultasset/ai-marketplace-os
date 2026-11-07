"""image_generation: image candidates and image provenance (SI7, ADR-0030 proposed)

Ledger row claimed in CANON.md with the Tech Lead's OK on 2026-11-07 (chat, V1 delegation).
Expand only, as migrator, in one transaction (`sql/0018/*`):

1. `image_candidates`: a tenant table, one row per proposed image (`white_background`, `fix`,
   `size_chart`), with its status, failure reason, stored object, fidelity scores, model run and
   cost. The partial unique cache index `uq_image_candidates_cache` holds every row except the
   retryable failures, so one source pays for one model call. `source_hash` is hidden from the
   read-only roles (§8.2); app_rw updates only the outcome columns. RLS ENABLE + FORCE with the
   0005 template.
2. `images` gains write-once provenance: `origin` (`seller|generated|rendered`), `parent_image_id`
   (FK, ON DELETE SET NULL of that column only) and `candidate_id` (no FK, CP-D3), guarded by
   `images_provenance_guard`.
3. `ck_job_runs_entity_type` gains `image_candidate` (NOT VALID, then VALIDATE).
4. `audit_allowlist` gains `image_candidate` (`kind`, `status`); `purge_tenant_order()` deletes
   `image_candidates` first.

Downgrade (local and CI only, RULES.md "No prod downgrade") restores the previous function bodies
and CHECK, then drops the columns and the table.

Revision ID: 0018_image_generation
Revises: 0015_image_public_copies
Create Date: 2026-11-07

"""

from collections.abc import Sequence
from pathlib import Path
from typing import Final

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0018_image_generation"
down_revision: str | Sequence[str] | None = "0015_image_public_copies"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQL_DIR: Final = Path(__file__).resolve().parents[1] / "sql" / "0018"
STEPS: Final = (
    "01_image_candidates",
    "02_images_provenance",
    "03_job_entity_type",
    "04_allowlist_purge",
)


def _run_sql(name: str) -> None:
    # Every colon is escaped (as in 0004, 0011 to 0015), so `text()` never reads one as a bind.
    op.execute(sa.text((SQL_DIR / name).read_text(encoding="utf-8").replace(":", "\\:")))


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    for step in STEPS:
        _run_sql(f"{step}.up.sql")


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    for step in reversed(STEPS):
        _run_sql(f"{step}.down.sql")
