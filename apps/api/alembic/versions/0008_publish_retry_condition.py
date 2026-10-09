"""publish_retry_condition: a retryable partial publish may be retried on the same version

DATABASE.md §5.3 `listing_submissions` `ls_guard` (:875) widened by S21 PLAN D1; CANON.md ledger
row 0008 (S21). One `CREATE OR REPLACE FUNCTION public.ls_guard()` and no table DDL, so it takes
no table lock beyond the catalog row: the `retryable_condition` justification also accepts a
previous `partially_accepted` or `rejected` submission whose ErrorReport is `retryable` and whose
every blocking (`severity = error`) item is retryable. Before this revision only a previous
`error` row qualified, so a transient field failure (one child's 429, an `auto_retry` code) could
never be retried on the same version. Everything else is 0004's body, unchanged.

The SQL lives in `alembic/sql/0008/`: `ls_guard.up.sql` (the widened body) and
`ls_guard.down.sql` (the parent's body, verbatim: 0004's, which no revision up to
`0007_facts_guard` replaced).

Revision ID: 0008_publish_retry_condition
Revises: 0007_facts_guard
Create Date: 2026-10-09

"""

from collections.abc import Sequence
from pathlib import Path
from typing import Final

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008_publish_retry_condition"
down_revision: str | Sequence[str] | None = "0007_facts_guard"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQL_DIR: Final = Path(__file__).resolve().parents[1] / "sql" / "0008"


def _run(name: str) -> None:
    # `:` is escaped so SQLAlchemy reads no bind parameter in the plpgsql body
    op.execute(sa.text((SQL_DIR / name).read_text(encoding="utf-8").replace(":", "\\:")))


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _run("ls_guard.up.sql")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _run("ls_guard.down.sql")
