"""integrity: the database backstops (maker-checker, append-only, status and publish guards,
privileges, display functions, purge and prune)

DATABASE.md rev 3 §8.2 ledger row 0004 and CANON.md ledger row 0004. The SQL is hand-written
from the DATABASE.md sections each file cites and lives in ordered file pairs under
`alembic/sql/0004/`: `NN_<name>.up.sql` and `NN_<name>.down.sql`. `upgrade()` runs the `.up.sql`
files in name order, `downgrade()` the `.down.sql` files in reverse name order, so each file's
downgrade undoes exactly what its upgrade created. The files are read at migrate time from this
checkout, never from an installed package. It runs as `migrator` (env.py), which owns every
object it creates.

Not in this revision: `facts_guard` and the dictionary loader (S13's own revision), RLS (0005).

Revision ID: 0004_integrity
Revises: 0003b_procrastinate
Create Date: 2026-10-06

"""

from collections.abc import Sequence
from pathlib import Path
from typing import Final

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004_integrity"
down_revision: str | Sequence[str] | None = "0003b_procrastinate"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQL_DIR: Final = Path(__file__).resolve().parents[1] / "sql" / "0004"


def sql_files(direction: str) -> list[Path]:
    """The `.up.sql` (or `.down.sql`) files of this revision, in name order."""
    return sorted(SQL_DIR.glob(f"*.{direction}.sql"), key=lambda path: path.name)


def _run(path: Path) -> None:
    # Every colon is escaped, so `text()` never reads `:name` (or `DB:1400` in a comment) as a
    # bind parameter; the rendered and executed SQL is the file as written.
    op.execute(sa.text(path.read_text(encoding="utf-8").replace(":", "\\:")))


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    for path in sql_files("up"):
        _run(path)


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    for path in reversed(sql_files("down")):
        _run(path)
