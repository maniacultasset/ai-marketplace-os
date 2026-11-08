"""invitee_email_unverified: mark accounts made from a copied invite link (V1 hardening #12)

CANON.md ledger row 0026 (claimed 2026-11-08 under the V1 delegation; chained on
`0025_jobruns_budget_idx`, which sits on `0023_image_role`). Expand only, as
migrator, in one transaction:

- `users.email_unverified boolean NOT NULL DEFAULT false` (a catalog-only change on PostgreSQL
  17, no rewrite). The invite accept with a password sets it on the account it creates: nobody
  has proved that the link's holder owns the address. A Google sign-in whose verified email
  matches the account clears it. Approving a listing needs it false
  (`app/services/maker_checker.py`), so a copied link alone never makes an approver.
- SELECT on the column for the read-only roles (it is a flag, not personal data). app_rw writes it
  through the 0000 default privileges, like every other unguarded `users` column.
- `lv_guard()` redefined (`sql/0026/01_lv_guard.up.sql`, 0004's body plus one check): an approver
  whose `email_unverified` is set is MKC01, the database backstop of the API rule.

Downgrade restores 0004's `lv_guard()` body, then drops the column (local and CI only).

Revision ID: 0026_invitee_email_unverified
Revises: 0025_jobruns_budget_idx
Create Date: 2026-11-08

"""

from collections.abc import Sequence
from pathlib import Path
from typing import Final

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0026_invitee_email_unverified"
down_revision: str | Sequence[str] | None = "0025_jobruns_budget_idx"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQL_DIR: Final = Path(__file__).resolve().parents[1] / "sql" / "0026"


def _run_sql(name: str) -> None:
    # Every colon is escaped (as in 0004, 0011 to 0022), so `text()` never reads one as a bind.
    op.execute(sa.text((SQL_DIR / name).read_text(encoding="utf-8").replace(":", "\\:")))


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.execute(
        "ALTER TABLE public.users ADD COLUMN email_unverified boolean NOT NULL DEFAULT false"
    )
    op.execute("GRANT SELECT (email_unverified) ON public.users TO app_ro, app_platform_ro")
    _run_sql("01_lv_guard.up.sql")


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    _run_sql("01_lv_guard.down.sql")
    op.execute("ALTER TABLE public.users DROP COLUMN email_unverified")
