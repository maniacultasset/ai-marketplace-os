"""analytics_trace: the job trace link and the four analytics read indexes

CANON.md ledger row 0009 (S23; claimed in parallel with S21's 0008_publish_retry_condition, which the
rebase puts underneath this one). Expand only, as migrator:

- `job_runs.traceparent text` (nullable, catalog-only, no rewrite): the W3C trace context of the
  request that enqueued the job, so the worker's job span is a child of that request's span. CHECK
  `ck_job_runs_traceparent_format` (`00-<32 hex>-<16 hex>-0[01]`, lowercase) added NOT VALID, then
  validated in its own transaction (SHARE UPDATE EXCLUSIVE, no write block).
- Four indexes for `GET /api/v1/analytics` (one per metric source), each built CONCURRENTLY in an
  autocommit block and preceded by `DROP INDEX CONCURRENTLY IF EXISTS`, so a rerun after an
  interrupted build replaces an INVALID index.

`lock_timeout` 5 s makes the brief ACCESS EXCLUSIVE of ADD COLUMN / ADD CONSTRAINT fail fast
instead of queueing behind a long reader. `job_runs` is a whole-table read-only grant (0003), so
`app_ro` and `app_platform_ro` read the new column with no new grant. Downgrade drops the four
indexes concurrently, then the constraint and the column.

Revision ID: 0009_analytics_trace
Revises: 0008_publish_retry_condition
Create Date: 2026-10-09

"""

from collections.abc import Sequence
from typing import Final

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009_analytics_trace"
down_revision: str | Sequence[str] | None = "0008_publish_retry_condition"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TRACEPARENT_PATTERN: Final = "^00-[0-9a-f]{32}-[0-9a-f]{16}-0[01]$"

# (name, definition after `ON`)
INDEXES: Final[tuple[tuple[str, str], ...]] = (
    (
        "ix_listing_versions_tenant_id_created_at_generated",
        "public.listing_versions (tenant_id, created_at) "
        "WHERE origin IN ('generation', 'regeneration')",
    ),
    (
        "ix_review_decisions_tenant_id_decided_at",
        "public.review_decisions (tenant_id, decided_at) INCLUDE (decision)",
    ),
    (
        "ix_validation_runs_tenant_id_created_at_first_pass",
        "public.validation_runs (tenant_id, created_at) INCLUDE (passed, marketplace) "
        "WHERE source = 'rules_engine' AND attempt = 1",
    ),
    (
        "ix_listing_submissions_tenant_id_responded_at",
        "public.listing_submissions (tenant_id, responded_at) INCLUDE (outcome, marketplace) "
        "WHERE responded_at IS NOT NULL",
    ),
)


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.execute("ALTER TABLE public.job_runs ADD COLUMN traceparent text")
    op.execute(
        "ALTER TABLE public.job_runs ADD CONSTRAINT ck_job_runs_traceparent_format "
        f"CHECK (traceparent IS NULL OR traceparent ~ '{TRACEPARENT_PATTERN}') NOT VALID"
    )
    with op.get_context().autocommit_block():
        # after the ADD's commit, so the ACCESS EXCLUSIVE is gone while it scans
        op.execute("ALTER TABLE public.job_runs VALIDATE CONSTRAINT ck_job_runs_traceparent_format")
        for name, definition in INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS public.{name}")
            op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {definition}")


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    with op.get_context().autocommit_block():
        for name, _definition in reversed(INDEXES):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS public.{name}")
    op.execute("SET lock_timeout = '5s'")
    op.execute("ALTER TABLE public.job_runs DROP CONSTRAINT ck_job_runs_traceparent_format")
    op.execute("ALTER TABLE public.job_runs DROP COLUMN traceparent")
