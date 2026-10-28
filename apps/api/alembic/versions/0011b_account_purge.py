"""account_purge: delete one marketplace account's data within 30 days of losing access

Amazon DPP 1.7 and the Flipkart equivalent. Functions only, no table or column change:
`purge_marketplace_account(t, a, force)` (`sql/0011b/01_account_purge.up.sql`), migrator only
(SECURITY INVOKER; the 0000 default privileges grant EXECUTE to nobody). It deletes the account's
submissions, every listing linked to it with that listing's tree, the account's human tasks and the
account row, writes a `marketplace_account.purged` tombstone and returns the submission object keys
for the caller to delete. The daily maintenance job (`app.workers.maintenance`) calls it for
accounts revoked or disconnected for 25 days.

The revision sits between 0011 and the 0012 that Google sign-in holds, so that claim is unchanged.
Downgrade (local and CI only, RULES.md "No prod downgrade") drops the function.

Revision ID: 0011b_account_purge
Revises: 0011_mfa_lockout
Create Date: 2026-10-28

"""

from collections.abc import Sequence
from pathlib import Path
from typing import Final

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011b_account_purge"
down_revision: str | Sequence[str] | None = "0011_mfa_lockout"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQL_DIR: Final = Path(__file__).resolve().parents[1] / "sql" / "0011b"


def _run_sql(name: str) -> None:
    # Every colon is escaped (as in 0004 and 0011), so `text()` never reads one as a bind parameter.
    op.execute(sa.text((SQL_DIR / name).read_text(encoding="utf-8").replace(":", "\\:")))


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    _run_sql("01_account_purge.up.sql")


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    _run_sql("01_account_purge.down.sql")
