"""product_type_definitions: Amazon Product Type Definitions, stored versioned (DW2)

T53 (docs/review/TEAM-QUESTIONS.md:87). Ledger row claimed in CANON.md with the Tech Lead's OK on
2026-11-03. Expand only, as migrator, in one transaction:

1. `product_type_definitions` (`sql/0014/01_*`): a platform table like `category_templates`
   (DATABASE.md:158): no `tenant_id`, no RLS, SELECT only for the app roles, written only by the
   migrator's `app.cli templates refresh|promote-definition`. One row per (marketplace id,
   product type, requirements, locale, version, schema digest); `ptd_one_current` keeps at most
   one current row per key. No personal column: the actor is in the audit row.
2. `product_type_definitions_guard`: content is immutable for every role, only `is_current`
   moves, and no row is deleted.
3. `audit_allowlist` (`sql/0014/02_*`) gains the entity `product_type_definition`.

Downgrade (local and CI only, RULES.md "No prod downgrade") restores 0004's allow-list body and
drops the table; the rows are refreshable from Amazon.

Revision ID: 0014_product_type_definitions
Revises: 0013_trusted_devices
Create Date: 2026-11-03

"""

from collections.abc import Sequence
from pathlib import Path
from typing import Final

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0014_product_type_definitions"
down_revision: str | Sequence[str] | None = "0013_trusted_devices"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQL_DIR: Final = Path(__file__).resolve().parents[1] / "sql" / "0014"


def _run_sql(name: str) -> None:
    # Every colon is escaped (as in 0004, 0011 to 0013), so `text()` never reads one as a bind.
    op.execute(sa.text((SQL_DIR / name).read_text(encoding="utf-8").replace(":", "\\:")))


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    _run_sql("01_product_type_definitions.up.sql")
    _run_sql("02_audit_allowlist.up.sql")


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    _run_sql("02_audit_allowlist.down.sql")
    _run_sql("01_product_type_definitions.down.sql")
