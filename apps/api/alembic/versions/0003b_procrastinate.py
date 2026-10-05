"""procrastinate: the pinned 3.10.0 schema, vendored and frozen, plus EXECUTE for app_rw

DATABASE.md §5.4 ("procrastinate tables") and §8.2 ledger row 0003b; ADR-0004 §1. The SQL is
`procrastinate/sql/schema.sql` of 3.10.0, copied byte for byte into `alembic/sql/` and never read
from the installed package at migrate time; a test compares it with the pinned library. It runs as
`migrator` (env.py), so the 4 tables, 18 functions, 2 enums, 1 composite type and 7 triggers are
migrator's. The hardened default privileges leave `app_rw` no EXECUTE on them, so the GRANT loop
gives it EXECUTE on every non-trigger procrastinate function (trigger functions fire without it);
table DML and sequence USAGE come from the 0000 default privileges. PUBLIC and the read-only roles
get nothing. The tables carry only `{job_run_id}` and have no RLS (allowlisted in the isolation
test). A later procrastinate upgrade is a new revision using its pre/post migration split.

Revision ID: 0003b_procrastinate
Revises: 0003_ops_tables
Create Date: 2026-10-05

"""

from collections.abc import Sequence
from pathlib import Path
from typing import Final

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003b_procrastinate"
down_revision: str | Sequence[str] | None = "0003_ops_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA_SQL: Final = Path(__file__).resolve().parents[1] / "sql" / "procrastinate-3.10.0-schema.sql"

GRANT_EXECUTE: Final = """
DO $$ DECLARE f regprocedure; BEGIN
  FOR f IN SELECT p.oid::regprocedure FROM pg_proc p
           WHERE p.proname LIKE 'procrastinate%' AND p.prorettype <> 'trigger'::regtype LOOP
    EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO app_rw', f);
  END LOOP;
END $$;
"""

# Catalog-driven, so nothing the vendored SQL created is missed: the tables (their indexes,
# sequences and triggers go with them), then the functions, then the enum and standalone
# composite types.
DROP_ALL: Final = r"""
DO $$ DECLARE r record; BEGIN
  FOR r IN SELECT c.oid::regclass AS name FROM pg_class c
           WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'r'
             AND c.relname LIKE 'procrastinate\_%' LOOP
    EXECUTE format('DROP TABLE %s CASCADE', r.name);
  END LOOP;
  FOR r IN SELECT p.oid::regprocedure AS name FROM pg_proc p
           WHERE p.pronamespace = 'public'::regnamespace
             AND p.proname LIKE 'procrastinate\_%' LOOP
    EXECUTE format('DROP FUNCTION %s', r.name);
  END LOOP;
  FOR r IN SELECT t.oid::regtype AS name FROM pg_type t
           WHERE t.typnamespace = 'public'::regnamespace AND t.typname LIKE 'procrastinate\_%'
             AND (t.typtype = 'e' OR (t.typtype = 'c' AND t.typrelid IN (
                  SELECT oid FROM pg_class WHERE relkind = 'c'))) LOOP
    EXECUTE format('DROP TYPE %s', r.name);
  END LOOP;
END $$;
"""


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
    op.execute(GRANT_EXECUTE)


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.execute(DROP_ALL)
