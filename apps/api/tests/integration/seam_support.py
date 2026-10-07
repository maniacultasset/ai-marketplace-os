"""Shared rows for the S14b seam and rules suites (T5-T13), written by the admin login.

Every rule body here validates as a wire `Rule` (the factories' default `error_code` "X" does not).
"""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from . import factories as f
from .jobs_support import insert_job

Conn = psycopg.Connection[tuple[Any, ...]]
CATEGORY = "ethnic_wear_kurta_sets"
FAR_FUTURE = "2099-01-01T00:00:00Z"


def max_length(limit: int = 200, **fields: Any) -> dict[str, Any]:
    return {
        "type": "max_length",
        "limit": limit,
        "message": "Title is {value} characters; the limit is {limit}.",
        "policy_ref": f.POLICY_REF,
        **fields,
    }


def banned(terms: list[str], **fields: Any) -> dict[str, Any]:
    return {
        "type": "banned_terms",
        "terms": terms,
        "message": "Remove {term}.",
        "pack": "brand",
        **fields,
    }


def pattern(regex: str, name: str = "sku_shape", **fields: Any) -> dict[str, Any]:
    return {
        "type": "pattern",
        "name": name,
        "regex": regex,
        "must": "not_match",
        "message": "Pattern {value}.",
        "policy_ref": f.POLICY_REF,
        **fields,
    }


def add_rule(
    conn: Conn,
    body: dict[str, Any],
    *,
    tenant_id: uuid.UUID | None = None,
    marketplace: str = "amazon",
    category: str | None = CATEGORY,
    field: str = "content.title",
    subject: str = "",
    enabled: bool = True,
    version: int = 1,
) -> uuid.UUID:
    return f.rule(
        conn,
        tenant_id,
        rule_def=body,
        marketplace=marketplace,
        category=category,
        field=field,
        rule_type=body["type"],
        subject=subject,
        pack=body.get("pack", "core"),
        enabled=enabled,
        version=version,
    )


def revision_of(conn: Conn, rule_id: uuid.UUID) -> int:
    row = conn.execute("SELECT revision FROM rules WHERE id = %s", [rule_id]).fetchone()
    assert row is not None
    return int(row[0])


def running_job(conn: Conn, tenant_id: uuid.UUID, **columns: Any) -> uuid.UUID:
    """A `running` attempt-1 `listing_generate` job with a live lease, on a new listing unless
    `listing_id` is given."""
    if "listing_id" not in columns:
        listing_id = f.listing(conn, tenant_id, category=CATEGORY)
        columns = {"listing_id": listing_id, "entity_id": listing_id, **columns}
    values: dict[str, Any] = {
        "kind": "listing_generate",
        "entity_type": "listing",
        "status": "running",
        "attempt": 1,
        "started_at": "2026-10-05T00:00:00Z",
        "lease_expires_at": FAR_FUTURE,
        **columns,
    }
    return insert_job(conn, tenant_id, **values)


def terminal_job(conn: Conn, tenant_id: uuid.UUID) -> uuid.UUID:
    listing_id = f.listing(conn, tenant_id, category=CATEGORY)
    return insert_job(
        conn,
        tenant_id,
        kind="listing_generate",
        entity_type="listing",
        entity_id=listing_id,
        listing_id=listing_id,
        status="succeeded",
        attempt=1,
        started_at="2026-10-05T00:00:00Z",
        finished_at="2026-10-05T00:01:00Z",
    )


def node_of(conn: Conn, job_id: uuid.UUID) -> str | None:
    row = conn.execute("SELECT node FROM job_runs WHERE id = %s", [job_id]).fetchone()
    assert row is not None
    return row[0]  # type: ignore[no-any-return]


def image_report(image_id: uuid.UUID, marketplace: str = "amazon") -> dict[str, Any]:
    return {
        "image_id": str(image_id),
        "marketplace": marketplace,
        "slot": "secondary",
        "pass": True,
        "checks": [{"check": "format", "method": "deterministic", "passed": True}],
        "issues": [],
        "processed_at": "2026-10-05T00:00:00Z",
    }


def add_image(
    conn: Conn, tenant_id: uuid.UUID, product_id: uuid.UUID, *marketplaces: str, **columns: Any
) -> uuid.UUID:
    """A live image of `product_id`; each named marketplace gets a stored report."""
    image_id = uuid.uuid4()
    reports = {m: image_report(image_id, m) for m in marketplaces}
    f.image(
        conn,
        tenant_id,
        product_id,
        id=image_id,
        image_report_json=Jsonb(reports) if reports else None,
        **columns,
    )
    return image_id


def add_template(
    conn: Conn,
    *,
    marketplace: str = "amazon",
    category: str = CATEGORY,
    version: int = 1,
    is_current: bool = True,
    attributes: dict[str, Any] | None = None,
) -> uuid.UUID:
    return f.insert(  # type: ignore[no-any-return]
        conn,
        "category_templates",
        marketplace=marketplace,
        category=category,
        version=version,
        is_current=is_current,
        product_type="KURTA",
        source="manual",
        attributes={
            "required_attributes": [],
            "optional_attributes": [],
            "specs": {},
            **(attributes or {}),
        },
    )


def add_product(conn: Conn, tenant_id: uuid.UUID, **columns: Any) -> uuid.UUID:
    return f.product(conn, tenant_id, category=CATEGORY, **columns)
