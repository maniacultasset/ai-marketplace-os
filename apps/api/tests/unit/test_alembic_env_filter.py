"""The autogenerate filter: procrastinate's own tables (revision 0003b) are not ours to compare,
so `alembic check` must never want to drop them (DATABASE.md:952)."""

from __future__ import annotations

import pytest
from alembic.runtime.environment import NameFilterType
from app.db.migrations import include_name

PROCRASTINATE_TABLES = (
    "procrastinate_jobs",
    "procrastinate_events",
    "procrastinate_periodic_defers",
    "procrastinate_workers",
)


@pytest.mark.parametrize("name", PROCRASTINATE_TABLES)
def test_include_name_hides_procrastinate_tables(name: str) -> None:
    assert include_name(name, "table", {"schema_name": None}) is False


@pytest.mark.parametrize("name", ["job_runs", "procrastinate", "listing_submissions"])
def test_include_name_keeps_ours(name: str) -> None:
    assert include_name(name, "table", {"schema_name": None}) is True


@pytest.mark.parametrize("type_", ["schema", "column", "index", "unique_constraint"])
def test_include_name_keeps_non_tables(type_: NameFilterType) -> None:
    assert include_name("procrastinate_jobs", type_, {}) is True


def test_include_name_keeps_none() -> None:
    # Alembic calls the hook with name None for the default schema.
    assert include_name(None, "schema", {}) is True
