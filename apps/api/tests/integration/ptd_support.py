"""DW2: store a Product Type Definition row straight into the test database (the admin login),
as `templates refresh` would, for the preview and publish suites."""

from __future__ import annotations

import json
import uuid
from typing import Any

from app.adapters.amazon.definitions import canonical_digest

KEY = ("A21TJRUUN4KGV", "KURTA", "LISTING_PRODUCT_ONLY", "en_IN")


def store(
    conn: Any,
    schema: dict[str, Any],
    *,
    version: str = "UFAKE0001",
    product_type: str = "KURTA",
    current: bool = True,
    enforced: str = "ENFORCED",
) -> uuid.UUID:
    marketplace_id, _, requirements, locale = KEY
    if current:
        conn.execute(
            "UPDATE product_type_definitions SET is_current = false WHERE marketplace_id = %s "
            "AND product_type = %s AND requirements = %s AND locale = %s AND is_current",
            [marketplace_id, product_type, requirements, locale],
        )
    row = conn.execute(
        "INSERT INTO product_type_definitions (marketplace, marketplace_id, product_type, "
        "requirements, locale, product_type_version, requirements_enforced, display_name, "
        "property_groups, schema_json, meta_schema_id, schema_digest, fetched_at, is_current) "
        "VALUES ('amazon', %s, %s, %s, %s, %s, %s, NULL, '{}'::jsonb, %s::jsonb, NULL, %s, now(), %s) "
        "RETURNING id",
        [
            marketplace_id,
            product_type,
            requirements,
            locale,
            version,
            enforced,
            json.dumps(schema),
            canonical_digest(schema),
            current,
        ],
    ).fetchone()
    assert row is not None
    return uuid.UUID(str(row[0]))


def promote(conn: Any, definition_id: uuid.UUID) -> None:
    conn.execute(
        "UPDATE product_type_definitions SET is_current = false WHERE is_current AND id <> %s",
        [definition_id],
    )
    conn.execute(
        "UPDATE product_type_definitions SET is_current = true WHERE id = %s", [definition_id]
    )
