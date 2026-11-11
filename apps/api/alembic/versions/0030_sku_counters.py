"""sku_counters: the per-brand SKU numbers behind "Make one for me" (client feedback 11 Nov)

Ledger row claimed in CANON.md under the owner's approval of the 11 Nov client feedback (item 3,
SKU ownership). Expand only, as migrator, in one transaction with `lock_timeout = '5s'`.

1. `sql/0030/01_sku_counters`: the tenant table (`code` `<BRAND>-<CATEGORY>`, `last_value` 1 to
   99,999,999 and only ever raised; FORCE RLS on the plain template; app_rw SELECT, INSERT and
   UPDATE with no DELETE; app_ro and app_platform_ro SELECT, as it holds no seller text).
2. `sql/0030/02_purge`: `purge_tenant_order()` is 0027's body plus `sku_counters`.

Downgrade (local and CI only, RULES.md "No prod downgrade") refuses while a counter exists, then
restores the 0027 purge body and drops the table.

Revision ID: 0030_sku_counters
Revises: 0029_admin_google_link
Create Date: 2026-11-11

"""

from collections.abc import Sequence
from pathlib import Path
from typing import Final

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0030_sku_counters"
# The single head on main after the admin Google link (0029_admin_google_link).
down_revision: str | Sequence[str] | None = "0029_admin_google_link"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQL_DIR: Final = Path(__file__).resolve().parents[1] / "sql" / "0030"
STEPS: Final = ("01_sku_counters", "02_purge")


def _run_sql(name: str) -> None:
    # Every colon is escaped (as in 0004, 0011 to 0029), so `text()` never reads one as a bind.
    op.execute(sa.text((SQL_DIR / name).read_text(encoding="utf-8").replace(":", "\\:")))


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    for step in STEPS:
        _run_sql(f"{step}.up.sql")


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    for step in reversed(STEPS):
        _run_sql(f"{step}.down.sql")
