"""image_role: the seller's Back and Close-up photos, and the size-chart read job (SI12)

Ledger row claimed in CANON.md under the V1 delegation on 2026-11-08. Expand only, as migrator,
in one transaction (`sql/0023/*`):

1. `images.role` (`back` | `close_up`, only on a `secondary` image; `ck_images_role`, NOT VALID
   then validated) and `images_one_role`: one live holder per (product, colourway, role).
2. `job_runs.entity_type` takes `size_chart_read` (an `image_compliance` job; JobKind stays closed).
3. `app_ro` and `app_platform_ro` read `images.role`.

Downgrade (local and CI only) refuses while any image has a role or any `size_chart_read` job
exists, else drops everything above.

Revision ID: 0023_image_role
Revises: 0022_autonomous_resolution
Create Date: 2026-11-08

"""

from collections.abc import Sequence
from pathlib import Path
from typing import Final

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0023_image_role"
# Chained on `0021_ai_credit_state` at build; re-chained on SI11's 0022_autonomous_resolution at
# the 2026-11-08 merge of main.
down_revision: str | Sequence[str] | None = "0022_autonomous_resolution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQL_DIR: Final = Path(__file__).resolve().parents[1] / "sql" / "0023"
STEPS: Final = ("01_image_role", "02_job_entity_type", "03_grants")


def _run_sql(name: str) -> None:
    # Every colon is escaped (as in 0004, 0011 to 0021), so `text()` never reads one as a bind.
    op.execute(sa.text((SQL_DIR / name).read_text(encoding="utf-8").replace(":", "\\:")))


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    for step in STEPS:
        _run_sql(f"{step}.up.sql")


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    for step in reversed(STEPS):
        _run_sql(f"{step}.down.sql")
