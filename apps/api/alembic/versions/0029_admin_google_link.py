"""admin_google_link: a platform admin may link a Google account (ADR-0020 amendment)

Ledger row claimed in CANON.md under the owner decision of 11 Nov 2026 (platform admins may sign
in with Google and always enter their Kuinn code afterwards). Expand only, as migrator, in one
transaction with `lock_timeout = '5s'`, no table change:

1. `sql/0029/01_link_google_sub`: `app_link_google_sub(uuid, text)` redefined from 0012's body
   without `AND NOT u.is_platform_admin`, so the link step (password, then code) can link an
   admin's Google account. Every other condition stays: only for `app.user_id`, a live user with
   a password and no Google account yet, and a Google account nobody else holds.

Downgrade (local and CI only, RULES.md "No prod downgrade") refuses while a platform admin has a
linked Google account, then restores 0012's body.

Revision ID: 0029_admin_google_link
Revises: 0028_photo_read_on_upload
Create Date: 2026-11-11

"""

from collections.abc import Sequence
from pathlib import Path
from typing import Final

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0029_admin_google_link"
# The single head on main after the V1.1 photo read (0028_photo_read_on_upload).
down_revision: str | Sequence[str] | None = "0028_photo_read_on_upload"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQL_DIR: Final = Path(__file__).resolve().parents[1] / "sql" / "0029"
STEPS: Final = ("01_link_google_sub",)


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
