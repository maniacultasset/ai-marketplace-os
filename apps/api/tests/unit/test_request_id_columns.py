"""S11 T11: every `request_id` column is stamped from the request context by a Python-side
default, with the DDL unchanged (Alembic compares no Python default; `alembic check` stays clean).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Column, Text

REQUEST_ID_TABLES = {
    "audit_log",
    "review_decisions",
    "validation_runs",
    "batches",
    "job_runs",
    "agent_runs",
    "human_tasks",
}
# The DDL nullability of each column (revisions 0002 and 0003): agent_runs is NOT NULL.
NOT_NULL = {"agent_runs"}


def _request_id_columns() -> dict[str, Column[Any]]:
    import app.db.models  # noqa: F401 - registers every table
    from app.db.base import metadata

    return {
        table.name: table.c.request_id
        for table in metadata.sorted_tables
        if "request_id" in table.c
    }


def test_every_request_id_column_stamped() -> None:
    from app.core.request_context import request_context

    columns = _request_id_columns()
    assert columns
    for name, column in columns.items():
        default = column.default
        assert default is not None, name
        assert default.is_callable, name
        call = default.arg  # type: ignore[attr-defined]
        with request_context("probe"):
            assert call(None) == "probe", name
        assert call(None) is None, name


def test_request_id_tables_cover_database_md() -> None:
    assert set(_request_id_columns()) >= REQUEST_ID_TABLES


def test_request_id_column_ddl_unchanged() -> None:
    for name, column in _request_id_columns().items():
        assert isinstance(column.type, Text), name
        assert column.nullable is (name not in NOT_NULL), name
        assert column.server_default is None, name
