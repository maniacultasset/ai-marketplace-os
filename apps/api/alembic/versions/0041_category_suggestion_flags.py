"""category_suggestion_flags: an honest category suggestion (client feedback 17 Nov)

A jeans product was suggested T-shirts. The stored suggestion (`products.category_suggestion`)
now also carries `not_listed` (the model ranked "none of these" first) and `looks_like` (the
seller's words name another group than the top candidate's). Expand only, as migrator, in one
transaction with `lock_timeout = '5s'`.

1. `sql/0041/01_category_suggestion_flags`: `category_suggestion_valid()` is 0027's body plus the
   two optional keys (`not_listed` boolean, `looks_like` null or one of topwear, bottomwear,
   ethnic, home, other). CREATE OR REPLACE: the CHECK is unchanged and existing rows stay valid.

Downgrade (local and CI only, RULES.md "No prod downgrade") removes both keys from every stored
suggestion, then restores the 0027 body.

Revision ID: 0041_category_suggestion_flags
Revises: 0040_sheet_photo_links
Create Date: 2026-11-17

"""

from collections.abc import Sequence
from pathlib import Path
from typing import Final

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0041_category_suggestion_flags"
down_revision: str | Sequence[str] | None = "0040_sheet_photo_links"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQL_DIR: Final = Path(__file__).resolve().parents[1] / "sql" / "0041"


def _run_sql(name: str) -> None:
    # Every colon is escaped (as in 0004, 0011 to 0040), so `text()` never reads one as a bind.
    op.execute(sa.text((SQL_DIR / name).read_text(encoding="utf-8").replace(":", "\\:")))


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    _run_sql("01_category_suggestion_flags.up.sql")


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    _run_sql("01_category_suggestion_flags.down.sql")
