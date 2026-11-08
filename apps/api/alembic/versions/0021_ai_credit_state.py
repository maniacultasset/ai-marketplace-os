"""ai_credit_state: the AI provider pause and the last OpenRouter key check (SI10)

Ledger row claimed in CANON.md under the V1 delegation on 2026-11-07. Expand only, as migrator,
in one transaction (`sql/0021/01_*`):

1. `ai_credit_state`, one platform row (`id = 1`, seeded): the pause reason and time, the last
   `/key` numbers and check time, and who resumed it. app_rw holds SELECT and column UPDATE only,
   under RLS ENABLE + FORCE (DATABASE.md `ai_credit_state`, the recorded departure from the
   SELECT-only platform-table rule).
2. `ai_credit_state_guard`: a BEFORE UPDATE trigger that refuses (GRD01) clearing or changing a
   set reason, or writing `resumed_*`, unless the session is migrator, a superuser or a platform
   admin acting as itself.
3. `app_ai_credit_auto_clear(new_remaining)`: the worker's SECURITY DEFINER clear (CP-D3).

Downgrade (local and CI only) drops the function, the table and the guard function.

Revision ID: 0021_ai_credit_state
Revises: 0019_gender_baby
Create Date: 2026-11-07

"""

from collections.abc import Sequence
from pathlib import Path
from typing import Final

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0021_ai_credit_state"
# The single head on main at merge time (0019 and 0020 are not on main yet).
down_revision: str | Sequence[str] | None = "0019_gender_baby"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQL_DIR: Final = Path(__file__).resolve().parents[1] / "sql" / "0021"


def _run_sql(name: str) -> None:
    # Every colon is escaped (as in 0004, 0011 to 0015), so `text()` never reads one as a bind.
    op.execute(sa.text((SQL_DIR / name).read_text(encoding="utf-8").replace(":", "\\:")))


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    _run_sql("01_ai_credit_state.up.sql")


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    _run_sql("01_ai_credit_state.down.sql")
