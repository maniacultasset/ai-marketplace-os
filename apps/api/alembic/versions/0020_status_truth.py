"""status_truth: not-buyable published listings and rejected photo values (SI9)

Ledger row claimed in CANON.md under the V1 delegation (Tech Lead via chat, 2026-11-07). Expand
only, as migrator, in one transaction (`sql/0020/01_*`):

1. `ck_published` also allows `marketplace_state = inactive`: on the marketplace, not buyable yet
   (NOT VALID, then VALIDATE).
2. `listing_display_status` maps `(published, inactive)` to `human_action_required`, and so does
   the `listing_status_display.human_action_required` column (same columns, same order).
3. `products.photo_rejections jsonb NOT NULL DEFAULT '{}'` ({V1 photo key: the rejected value})
   with `ck_products_photo_rejections` over the new `photo_rejections_valid()` function.

Downgrade (local and CI only, RULES.md "No prod downgrade") refuses while a `(published,
inactive)` listing exists, then restores the 0004 bodies and the old CHECK, and drops the column,
the constraint and the function.

Revision ID: 0020_status_truth
Revises: 0015_image_public_copies
Create Date: 2026-11-07

"""

from collections.abc import Sequence
from pathlib import Path
from typing import Final

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0020_status_truth"
down_revision: str | Sequence[str] | None = "0015_image_public_copies"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQL_DIR: Final = Path(__file__).resolve().parents[1] / "sql" / "0020"


def _run_sql(name: str) -> None:
    # Every colon is escaped (as in 0004, 0011 to 0015), so `text()` never reads one as a bind.
    op.execute(sa.text((SQL_DIR / name).read_text(encoding="utf-8").replace(":", "\\:")))


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    _run_sql("01_status_truth.up.sql")


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    _run_sql("01_status_truth.down.sql")
