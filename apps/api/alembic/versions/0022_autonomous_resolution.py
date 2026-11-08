"""autonomous_resolution: the resolution engine's status edges and the marketplace dry run (SI11)

Ledger row claimed in CANON.md under the V1 delegation (Tech Lead via chat, 2026-11-08; ADR-0028
proposed). Expand only, as migrator, in one transaction (`sql/0022/01_*`):

1. The event vocabulary gains `auto_fix` (both CHECKs, NOT VALID then VALIDATE), with 9 worker
   edges: `auto_fix` -> `generating` from validated, needs_review, approved and published (the
   last two void the approval), from failed after a publish failure or rejection, and from
   blocked/validation_errors; `corrections_exhausted` -> blocked from validated, needs_review and
   failed (the 3-cycle cap, invariant 9).
2. `listings_status_events()`: `auto_fix`, like `generate`, needs a queued `listing_generate` job.
3. `listings.remote_check_version_id`, `remote_check_status` (pending, clean, issues, failed) and
   `remote_check_at`: the marketplace's dry-run verdict on one version, with
   `ck_listings_remote_check` and the deferrable `(id, remote_check_version_id)` version FK.
4. The tenant table `listing_repairs` (`sql/0022/02_*`): adapter-side repairs, one live row per
   (listing, item, attribute), one try per (listing, issue key, rule), retired once; RLS template;
   `audit_allowlist` (+ `listing_repair`) and `purge_tenant_order()` redefined from 0018.
5. The platform view `marketplace_error_catalog` (`sql/0022/03_*`): counts per marketplace code,
   no tenant id and no message text, readable only by `app_platform_ro`.

Downgrade (local and CI only, RULES.md "No prod downgrade") refuses while a listing's last event
is `auto_fix` or a `listing_repairs` row exists, then restores the previous bodies and CHECKs.

Revision ID: 0022_autonomous_resolution
Revises: 0021_ai_credit_state
Create Date: 2026-11-08

"""

from collections.abc import Sequence
from pathlib import Path
from typing import Final

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0022_autonomous_resolution"
down_revision: str | Sequence[str] | None = "0021_ai_credit_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQL_DIR: Final = Path(__file__).resolve().parents[1] / "sql" / "0022"
UP: Final = ("01_status.up.sql", "02_repairs.up.sql", "03_catalog.up.sql")
DOWN: Final = ("03_catalog.down.sql", "02_repairs.down.sql", "01_status.down.sql")


def _run_sql(name: str) -> None:
    # Every colon is escaped (as in 0004, 0011 to 0020), so `text()` never reads one as a bind.
    op.execute(sa.text((SQL_DIR / name).read_text(encoding="utf-8").replace(":", "\\:")))


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    for name in UP:
        _run_sql(name)


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    for name in DOWN:
        _run_sql(name)
