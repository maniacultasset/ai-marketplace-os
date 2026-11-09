"""v1_1_assist: the category classifier, the column mapper and their tasks and jobs (SI14)

Ledger row claimed in CANON.md under the V1.1 delegation (conductor, chat 2026-11-09); D3 (a
nullable product category) approved by the user directly the same day. Expand only, as migrator,
in one transaction with `lock_timeout = '5s'`.

1. `sql/0027/01_category_nullable`: `products.category` DROP NOT NULL (catalog-only).
2. `sql/0027/02_category_suggestion`: `products.category_suggestion jsonb` with
   `category_suggestion_valid()` behind a NOT VALID + VALIDATE CHECK.
3. `sql/0027/03_task_kind`: `ck_human_tasks_kind` takes `category_review` (a product, no fact path
   and no account), and one live review per product (`human_tasks_one_category_review`).
   Squawk flags the index built without CONCURRENTLY: accepted, no row can match a new kind and the
   table is small in v1 (the 0023 precedent).
4. `sql/0027/04_job_entity_type`: `ck_job_runs_entity_type` is the head's list (0024's, with
   `error_proposal`) plus `category_suggestion` and `column_map` (JobKind stays closed).
5. `sql/0027/05_import_column_maps`: the tenant table (FORCE RLS, no DELETE for app_rw, `samples`
   never granted to the read-only roles).
6. `sql/0027/06_imports_column_map_id`: `imports.column_map_id` (soft reference).
7. `sql/0027/07_purge`: `purge_tenant_order()` is 0022's body plus `import_column_maps`.
No column grant for `products.category_suggestion`: app_ro and app_platform_ro hold table-level
SELECT on `products` (as on `description`), so a column grant would change nothing.

Downgrade (local and CI only, RULES.md "No prod downgrade") refuses while a product without a
category, a `category_review` task, a `category_suggestion` or `column_map` job, or a column map
exists, then restores the parent's entity list, the 0002 kind list and the 0022 purge body.

Revision ID: 0027_v1_1_assist
Revises: 0024_brief_gaps
Create Date: 2026-11-09

"""

from collections.abc import Sequence
from pathlib import Path
from typing import Final

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0027_v1_1_assist"
# The single head on main after SI13 #116 merged (chain 0023 -> 0025 -> 0026 -> 0024).
down_revision: str | Sequence[str] | None = "0024_brief_gaps"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQL_DIR: Final = Path(__file__).resolve().parents[1] / "sql" / "0027"
STEPS: Final = (
    "01_category_nullable",
    "02_category_suggestion",
    "03_task_kind",
    "04_job_entity_type",
    "05_import_column_maps",
    "06_imports_column_map_id",
    "07_purge",
)


def _run_sql(name: str) -> None:
    # Every colon is escaped (as in 0004, 0011 to 0024), so `text()` never reads one as a bind.
    op.execute(sa.text((SQL_DIR / name).read_text(encoding="utf-8").replace(":", "\\:")))


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    for step in STEPS:
        _run_sql(f"{step}.up.sql")


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    for step in reversed(STEPS):
        _run_sql(f"{step}.down.sql")
