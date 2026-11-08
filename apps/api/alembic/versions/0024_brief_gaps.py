"""brief_gaps: the AI error-map proposals, the child-axis repair rule and the diagnosis job (SI13)

Ledger row claimed in CANON.md under the V1 delegation (Tech Lead via chat, 2026-11-08). Expand
only, as migrator, in one transaction. No transition edge: the recategorise move after a rejection
rides the existing user `edit` edge (failed -> blocked).

1. `sql/0024/01_checks`: `ck_listing_repairs_rule` takes `fill_variant_axis`;
   `ck_job_runs_entity_type` takes `error_proposal` (a `brand_embed` diagnosis job; JobKind stays
   closed); `audit_allowlist()` is 0022's body plus `marketplace_error_proposal` (status).
2. `sql/0024/02_proposals`: the platform table `marketplace_error_proposals` (no tenant id, no
   RLS), its drafting guard (the worker never approves; only the migrator reviews; reviewed rows
   are immutable) and column grants.

Downgrade (local and CI only, RULES.md "No prod downgrade") refuses while a proposal, a
`fill_variant_axis` repair or an `error_proposal` job exists, then restores the 0023 CHECKs and
the 0022 allowlist body.

Revision ID: 0024_brief_gaps
Revises: 0023_image_role
Create Date: 2026-11-08

"""

from collections.abc import Sequence
from pathlib import Path
from typing import Final

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0024_brief_gaps"
down_revision: str | Sequence[str] | None = "0023_image_role"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQL_DIR: Final = Path(__file__).resolve().parents[1] / "sql" / "0024"
STEPS: Final = ("01_checks", "02_proposals")


def _run_sql(name: str) -> None:
    # Every colon is escaped (as in 0004, 0011 to 0023), so `text()` never reads one as a bind.
    op.execute(sa.text((SQL_DIR / name).read_text(encoding="utf-8").replace(":", "\\:")))


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    for step in STEPS:
        _run_sql(f"{step}.up.sql")


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    for step in reversed(STEPS):
        _run_sql(f"{step}.down.sql")
