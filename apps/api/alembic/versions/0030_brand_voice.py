"""brand_voice: the seller's answers to "Tell Kuinn about your brand" (client feedback item 19)

Claimed in CANON.md under the V1.1 delegation (client feedback 11 Nov 2026, item 19). Expand only,
as migrator, in one transaction with `lock_timeout = '5s'`.

1. `sql/0030/01_brand_voice`: `brand_profiles.brand_voice jsonb` (nullable, no default: catalog
   only) behind `ck_brand_profiles_brand_voice` (a JSON object of at most 16 KiB; the API's
   `BrandVoice` model holds the shape and the per-field caps), NOT VALID, then validated.
2. `sql/0030/02_audit_allowlist`: `audit_allowlist()` is 0024's body with `brand_voice` added to
   `brand_profile` (the audit row names the changed voice fields, never their text).

Downgrade (local and CI only, RULES.md "No prod downgrade") refuses while a brand voice is
stored, then restores 0024's allow-list and drops the column.

Revision ID: 0030_brand_voice
Revises: 0029_admin_google_link
Create Date: 2026-11-11

"""

from collections.abc import Sequence
from pathlib import Path
from typing import Final

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0030_brand_voice"
# The single head on main after the admin Google link (0029_admin_google_link).
down_revision: str | Sequence[str] | None = "0029_admin_google_link"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQL_DIR: Final = Path(__file__).resolve().parents[1] / "sql" / "0030"
STEPS: Final = ("01_brand_voice", "02_audit_allowlist")


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
