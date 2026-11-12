"""auto_fix_enabled: "Let Kuinn fix safe listing errors automatically", and held repairs (item 20)

Client feedback 11 Nov 2026, item 20 bullet 3 ("safe, high-confidence fixes can be applied
automatically within authorised permissions"), under the V1 generic-decision delegation. Named
`0038_d5` in its lane; renumbered and chained when the release train takes it. Expand only, as
migrator, in one transaction with `lock_timeout = '5s'`:

1. `sql/0038/01_columns`: `tenants.auto_fix_enabled boolean NOT NULL DEFAULT true` (on, so every
   brand keeps today's behaviour). app_rw UPDATE on that column only; the service lets only an
   active owner change it, after a step-up (`app/services/tenants.py`). Catalog-only add.
2. `sql/0038/02_audit_allowlist`: 0033's `audit_allowlist` plus `auto_fix_enabled` on `tenant`, so
   the `tenant.auto_fix_enabled_changed` row keeps the value before and after.
3. `sql/0038/03_held_repairs`: `listing_repairs.held boolean NOT NULL DEFAULT false`. A repair
   found while the setting is off is stored held (a suggested fix the adapter never applies);
   the seller's "Apply fixes" releases it once. `listing_repairs_guard()` is 0022's body plus one
   rule: `held` only goes from true to false. app_rw UPDATE and the read roles' SELECT on it.

Downgrade (local and CI only, RULES.md "No prod downgrade") restores 0022's repair guard and
0033's allow-list and drops both columns.

Revision ID: 0038_d5
Revises: 0037_signup_info_request
Create Date: 2026-11-12

"""

from collections.abc import Sequence
from pathlib import Path
from typing import Final

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0038_d5"
# Chained on the wave A2 train's head; the release train renumbers it.
down_revision: str | Sequence[str] | None = "0037_signup_info_request"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQL_DIR: Final = Path(__file__).resolve().parents[1] / "sql" / "0038"
STEPS: Final = ("01_columns", "02_audit_allowlist", "03_held_repairs")


def _run_sql(name: str) -> None:
    # Every colon is escaped (as in 0004, 0011 to 0028), so `text()` never reads one as a bind.
    op.execute(sa.text((SQL_DIR / name).read_text(encoding="utf-8").replace(":", "\\:")))


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    for step in STEPS:
        _run_sql(f"{step}.up.sql")


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    for step in reversed(STEPS):
        _run_sql(f"{step}.down.sql")
