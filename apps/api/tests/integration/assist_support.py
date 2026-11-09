"""SI14 test helpers: current category templates, the assist switches and the stored suggestion."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from psycopg.types.json import Jsonb

from .jobs_support import Conn, count

SWITCHES = {
    "category": "CATEGORY_SUGGEST_ENABLED",
    "columns": "COLUMN_MAP_AI_ENABLED",
    "lamps": "CATEGORY_LAMPS_ENABLED",
    "photo_qa": "PHOTO_QA_ENABLED",
}


def set_switch(monkeypatch: pytest.MonkeyPatch, name: str, *, on: bool) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv(SWITCHES[name], "true" if on else "false")
    get_settings.cache_clear()


def seed_templates(conn: Conn, *categories: str, marketplace: str = "amazon") -> None:
    """One current manual template per category (what the classifier may offer)."""
    for category in categories:
        conn.execute(
            "INSERT INTO category_templates (marketplace, category, version, is_current, "
            "product_type, source, attributes) VALUES (%s, %s, 1, true, %s, 'manual', %s) "
            "ON CONFLICT DO NOTHING",
            [
                marketplace,
                category,
                category.upper(),
                Jsonb(
                    {
                        "required_attributes": ["fabric", "color_name"],
                        "optional_attributes": [],
                        "specs": {},
                    }
                ),
            ],
        )


def suggestion(conn: Conn, product_id: uuid.UUID) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT category_suggestion FROM products WHERE id = %s", [product_id]
    ).fetchone()
    assert row is not None
    return row[0]  # type: ignore[no-any-return]


def suggest_jobs(conn: Conn, product_id: uuid.UUID) -> int:
    return count(
        conn,
        "SELECT count(*) FROM job_runs WHERE entity_type = 'category_suggestion' "
        "AND entity_id = %s",
        [product_id],
    )


def review_tasks(conn: Conn, product_id: uuid.UUID, status: str | None = None) -> int:
    return count(
        conn,
        "SELECT count(*) FROM human_tasks WHERE kind = 'category_review' AND product_id = %s "
        "AND (%s::text IS NULL OR status = %s)",
        [product_id, status, status],
    )
