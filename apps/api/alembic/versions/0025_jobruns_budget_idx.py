"""jobruns_budget_idx: the job_runs indexes of the AI budget admission (V1 hardening #10)

CANON.md ledger row 0025 (claimed 2026-11-08 under the V1 delegation; chained on SI12's
`0023_image_role`, the single head on main at the 2026-11-08 merge, re-chained from
`0022_autonomous_resolution`; SI13's 0024 merges after this and is re-chained onto 0026).
Expand only, as migrator, no table change.

Every AI claim sums today's `cost_reserved_usd` over the reserving job kinds
(`app/repositories/budget.py`, `RESERVING_KINDS`), per tenant and across all tenants, while it
holds the budget lock. With no index that is a scan of `job_runs`, which grows by about a row per
listing per day. Two partial indexes match the `agent_runs` pair (0002):

- `ix_job_runs_budget_started_at` on `(started_at) INCLUDE (tenant_id, cost_reserved_usd)`, the
  platform sum (`global_state`);
- `ix_job_runs_budget_tenant_started_at` on `(tenant_id, started_at) INCLUDE (cost_reserved_usd)`,
  the tenant sum (`BudgetRepository.tenant_state`);

both `WHERE kind IN ('listing_generate', 'brand_embed', 'image_compliance')`. Each is built
CONCURRENTLY in an autocommit block, preceded by `DROP INDEX CONCURRENTLY IF EXISTS`, so a rerun
after an interrupted build replaces an INVALID index. Downgrade drops both concurrently.

Revision ID: 0025_jobruns_budget_idx
Revises: 0023_image_role
Create Date: 2026-11-08

"""

from collections.abc import Sequence
from typing import Final

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0025_jobruns_budget_idx"
# The single head on main at the 2026-11-08 merge (0022_autonomous_resolution at build).
down_revision: str | Sequence[str] | None = "0023_image_role"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RESERVING: Final = "kind IN ('listing_generate', 'brand_embed', 'image_compliance')"

# (name, definition after `ON`)
INDEXES: Final[tuple[tuple[str, str], ...]] = (
    (
        "ix_job_runs_budget_started_at",
        f"public.job_runs (started_at) INCLUDE (tenant_id, cost_reserved_usd) WHERE {RESERVING}",
    ),
    (
        "ix_job_runs_budget_tenant_started_at",
        f"public.job_runs (tenant_id, started_at) INCLUDE (cost_reserved_usd) WHERE {RESERVING}",
    ),
)


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    with op.get_context().autocommit_block():
        for name, definition in INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS public.{name}")
            op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {definition}")


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    with op.get_context().autocommit_block():
        for name, _definition in reversed(INDEXES):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS public.{name}")
