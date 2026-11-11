"""section_locks: the seller's field locks and the generation run's reason and scope

Ledger row claimed in CANON.md under the client feedback delegation (11 Nov 2026, items 14 and
16: save without rerunning the agent, regenerate one section, keep the seller's edits). Expand
only, as migrator, in one transaction with `lock_timeout = '5s'`.

1. `sql/0033/01_listing_locks`: `listings.locked_paths text[] NOT NULL DEFAULT '{}'` (the paths
   the seller edited; no regenerate changes them) and `ck_listings_locked_paths` (at most 256),
   NOT VALID, then validated.
2. `sql/0033/02_agent_run_reason`: `agent_runs.run_reason` (`generate`, `regenerate`,
   `section_regenerate`, `answers_applied`, `resolution`, or NULL) and `agent_runs.run_scope`
   (the paths the run could change; NULL for the whole listing), each with its CHECK, NOT VALID,
   then validated.

Downgrade (local and CI only, RULES.md "No prod downgrade") drops the columns and checks.

Revision ID: 0033_section_locks
Revises: 0032_owner_self_approval
Create Date: 2026-11-11

"""

from collections.abc import Sequence
from pathlib import Path
from typing import Final

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0033_section_locks"
# The single head on main after the owner self-approval setting (0032_owner_self_approval).
down_revision: str | Sequence[str] | None = "0032_owner_self_approval"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQL_DIR: Final = Path(__file__).resolve().parents[1] / "sql" / "0033"
STEPS: Final = ("01_listing_locks", "02_agent_run_reason")


def _run_sql(name: str) -> None:
    # Every colon is escaped (as in 0004, 0011 to 0032), so `text()` never reads one as a bind.
    op.execute(sa.text((SQL_DIR / name).read_text(encoding="utf-8").replace(":", "\\:")))


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    for step in STEPS:
        _run_sql(f"{step}.up.sql")


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    for step in reversed(STEPS):
        _run_sql(f"{step}.down.sql")
