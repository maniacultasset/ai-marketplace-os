"""Scratch Alembic revisions for the integration and Cloud SQL mimic tests. They are written
under `tmp_path`, never into apps/api/alembic/versions/ (no ledger id is claimed in S5)."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

LOCK_TIMEOUT_STMT = "SET lock_timeout = '5s'"


def write_revision(
    versions: Path,
    rev: str,
    upgrade_sql: Sequence[str],
    downgrade_sql: Sequence[str],
    *,
    down_revision: str | None = None,
) -> Path:
    """A minimal revision whose functions start, like the template's, with the lock_timeout."""

    def body(statements: Sequence[str]) -> str:
        lines = [LOCK_TIMEOUT_STMT, *statements]
        return "\n".join(f"    op.execute({sql!r})" for sql in lines)

    versions.mkdir(parents=True, exist_ok=True)
    path = versions / f"{rev}_scratch.py"
    path.write_text(
        f'"""scratch revision {rev}"""\n\n'
        "from alembic import op\n\n"
        f"revision = {rev!r}\n"
        f"down_revision = {down_revision!r}\n"
        "branch_labels = None\n"
        "depends_on = None\n\n\n"
        f"def upgrade() -> None:\n{body(upgrade_sql)}\n\n\n"
        f"def downgrade() -> None:\n{body(downgrade_sql)}\n",
        encoding="utf-8",
    )
    return path
