"""variant_photo_match: the job entity of a photo match (V1.1, client feedback item 9)

Ledger row claimed in CANON.md under the V1.1 delegation (client feedback 11 Nov 2026: check that a
variant's photo shows this product in this colour). Expand only, as migrator, in one transaction
with `lock_timeout = '5s'`.

1. `sql/0033/01_job_entity_type`: `ck_job_runs_entity_type` is the head's list (0032's, unchanged from 0028) plus
   `photo_match` (an `image_compliance` job about one image; JobKind stays closed). NOT VALID,
   then validated.

The match result itself is stored on the image as `images.metadata.photo_match` (as SI12 stores
`metadata.size_chart_read`), so no column is added.

Downgrade (local and CI only, RULES.md "No prod downgrade") refuses while a `photo_match` job
exists, then restores the 0032 head's entity list (0028's; 0029 to 0032 left it alone).

Revision ID: 0033_variant_photo_match
Revises: 0032_owner_self_approval
Create Date: 2026-11-11

"""

from collections.abc import Sequence
from pathlib import Path
from typing import Final

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0033_variant_photo_match"
# The single head on main after the owner self-approval setting (0032_owner_self_approval).
down_revision: str | Sequence[str] | None = "0032_owner_self_approval"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQL_DIR: Final = Path(__file__).resolve().parents[1] / "sql" / "0033"
STEPS: Final = ("01_job_entity_type",)


def _run_sql(name: str) -> None:
    # Every colon is escaped (as in 0004, 0011 to 0032), so `text()` never reads one as a bind.
    op.execute(sa.text((SQL_DIR / name).read_text(encoding="utf-8").replace(":", "\\:")))


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    for step in STEPS:
        _run_sql(f"{step}.up.sql")


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    for step in reversed(STEPS):
        _run_sql(f"{step}.down.sql")
