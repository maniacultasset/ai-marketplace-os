"""photo_read_on_upload: the job entity of a photo read on upload (V1.1)

Ledger row claimed in CANON.md under the V1.1 delegation (client input 8 Nov 2026: read photo
details as soon as a front, back or close-up photo is uploaded). Expand only, as migrator, in one
transaction with `lock_timeout = '5s'`.

1. `sql/0028/01_job_entity_type`: `ck_job_runs_entity_type` is the head's list (0027's) plus
   `photo_facts_read` (an `image_compliance` job about a product; JobKind stays closed). NOT
   VALID, then validated.

Downgrade (local and CI only, RULES.md "No prod downgrade") refuses while a `photo_facts_read`
job exists, then restores the 0027 entity list.

Revision ID: 0028_photo_read_on_upload
Revises: 0027_v1_1_assist
Create Date: 2026-11-09

"""

from collections.abc import Sequence
from pathlib import Path
from typing import Final

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0028_photo_read_on_upload"
# The single head on main after SI14 (0027_v1_1_assist).
down_revision: str | Sequence[str] | None = "0027_v1_1_assist"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQL_DIR: Final = Path(__file__).resolve().parents[1] / "sql" / "0028"
STEPS: Final = ("01_job_entity_type",)


def _run_sql(name: str) -> None:
    # Every colon is escaped (as in 0004, 0011 to 0027), so `text()` never reads one as a bind.
    op.execute(sa.text((SQL_DIR / name).read_text(encoding="utf-8").replace(":", "\\:")))


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    for step in STEPS:
        _run_sql(f"{step}.up.sql")


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    for step in reversed(STEPS):
        _run_sql(f"{step}.down.sql")
