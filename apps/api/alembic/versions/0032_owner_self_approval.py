"""owner_self_approval: owners may approve their own listings, per brand, off by default (ADR-0031)

CANON.md ledger row 0032 (claimed 2026-11-11 under the owner's decision of 11 Nov 2026; chained on
`0031_signup_status_handle` in the wave A release train,
after 0030 and 0031). Expand only, as migrator, in
one transaction with `lock_timeout = '5s'`:

1. `sql/0032/01_columns`: `tenants.owner_self_approval boolean NOT NULL DEFAULT false` (app_rw
   UPDATE on that column; the service lets only an active, verified owner change it, after a
   step-up); `listing_versions.self_approved boolean NOT NULL DEFAULT false`, written only by
   `lv_guard` (app_rw holds neither INSERT nor UPDATE on it), with
   `ck_listing_versions_self_approved` (only an approved row carries it); and `ck_maker_checker`
   rebuilt to admit a self-approved row. `review_decisions` is unchanged. Catalog-only adds.
2. `sql/0032/02_lv_guard`: `lv_guard()` is 0026's body plus one exception to the maker and chain
   checks: an active `owner` approver while the tenant's setting is on (the API rule,
   `app/services/maker_checker.py`), the setting read `FOR SHARE` so a concurrent toggle and an
   approval serialize. It sets `self_approved`. Role, active, email and current-version checks
   are unchanged.
3. `sql/0032/03_audit_allowlist`: 0024's `audit_allowlist` plus `owner_self_approval` on `tenant`.

Downgrade (local and CI only, RULES.md "No prod downgrade") refuses while a self-approval is
recorded, then restores 0024's `audit_allowlist` and 0026's `lv_guard` and drops the columns.

Revision ID: 0032_owner_self_approval
Revises: 0031_signup_status_handle
Create Date: 2026-11-11

"""

from collections.abc import Sequence
from pathlib import Path
from typing import Final

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0032_owner_self_approval"
# Chained on 0031_signup_status_handle in the wave A release train.
down_revision: str | Sequence[str] | None = "0031_signup_status_handle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQL_DIR: Final = Path(__file__).resolve().parents[1] / "sql" / "0032"
STEPS: Final = ("01_columns", "02_lv_guard", "03_audit_allowlist")


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
