"""team_access: copied reset links, trial recovery and the Google invitee (SI5, ADR-0029)

Ledger row claimed in CANON.md with the Tech Lead's OK on 2026-11-07. Expand only, as migrator, in
one transaction (`sql/0017/01_*` to `04_*`):

1. `password_reset_links`: a tenant table under FORCE RLS (the 0005 template plus the
   `password_reset_links_hash` SELECT branch), a 32-byte token hash, a 24-hour cap, one live link
   per member; app_rw has no TRUNCATE, UPDATE only `used_at, revoked_at`, and keeps DELETE for the
   retention prune; the read-only roles never read the hash.
2. `activate_tenant` and `extend_tenant_trial` (SECURITY DEFINER, staff or a platform admin) and
   the `tenants_platform_admin` SELECT policy. `set_tenant_lifecycle` and `expire_trials` are
   unchanged.
3. `app_create_google_invitee` (SECURITY DEFINER): the password-less Google user of an invite.
4. `audit_allowlist` (+ `invitation` {role}), `purge_tenant_order()` (+ `password_reset_links`)
   and `redact_user` (+ the user's reset links), each from its newest body.

Downgrade (local and CI only, RULES.md "No prod downgrade") restores the parents' bodies verbatim,
then drops the Google definer, the recovery definers and the policy, then the table.

Revision ID: 0017_team_access
Revises: 0015_image_public_copies
Create Date: 2026-11-07

"""

from collections.abc import Sequence
from pathlib import Path
from typing import Final

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0017_team_access"
down_revision: str | Sequence[str] | None = "0015_image_public_copies"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQL_DIR: Final = Path(__file__).resolve().parents[1] / "sql" / "0017"
PARTS: Final = ("01_reset_links", "02_tenant_recovery", "03_google_invitee", "04_allowlist_erasure")


def _run_sql(name: str) -> None:
    # Every colon is escaped (as in 0004, 0011 to 0015), so `text()` never reads one as a bind.
    op.execute(sa.text((SQL_DIR / name).read_text(encoding="utf-8").replace(":", "\\:")))


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    for part in PARTS:
        _run_sql(f"{part}.up.sql")


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    for part in reversed(PARTS):
        _run_sql(f"{part}.down.sql")
