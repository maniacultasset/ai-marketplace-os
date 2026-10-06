"""Shared helpers for the S12 product suites (CODE-PLAN S12 T3-T12)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg

Conn = psycopg.Connection[tuple[Any, ...]]


def seed_dictionary(conn: Conn) -> None:
    """The S12 dictionary rows (`seed_ops.ATTRIBUTE_DEFINITION_ROWS`) on the admin connection,
    left alone when already stored, as the seed itself does."""
    from app.db.seed_ops import ATTRIBUTE_DEFINITION_ROWS

    for row in ATTRIBUTE_DEFINITION_ROWS:
        columns = list(row)
        conn.execute(
            f"INSERT INTO attribute_definitions ({', '.join(columns)}) "  # noqa: S608
            f"VALUES ({', '.join(['%s'] * len(columns))}) ON CONFLICT (key) DO NOTHING",
            [row[c] for c in columns],
        )
    conn.execute(
        "UPDATE attribute_definitions SET enabled = true WHERE key IN ('color', 'fabric_type')"
    )


@contextmanager
def statements() -> Iterator[list[str]]:
    """Every SQL statement the app's engine sends while the block runs."""
    from app.db.session import get_engine
    from sqlalchemy import event

    seen: list[str] = []

    def record(_c: Any, _cur: Any, statement: str, *_rest: Any) -> None:
        seen.append(statement)

    engine = get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", record)


def as_json(value: Any) -> str:
    return json.dumps(value)
