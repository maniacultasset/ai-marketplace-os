"""SI13 T15 (AC15; P:B4, D7; CP-D12): `errors value-maps` aggregates every tenant's
refused-then-live values in memory and prints counts and enum values only."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from app.cli import main

from . import factories as f
from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]


def _refused_then_live(world: GenWorld, slug: str, v: str, w: str) -> tuple[uuid.UUID, str]:
    conn = world.conn
    tenant = world.tenant(slug)
    template = conn.execute(
        "SELECT id FROM category_templates WHERE marketplace = 'amazon' "
        "AND category = 'ethnic_wear_kurta_sets' AND is_current"
    ).fetchone()
    assert template is not None
    with conn.transaction():
        conn.execute("SET LOCAL session_replication_role = replica")
        product = f.product(conn, tenant)
        listing = f.listing(conn, tenant, product)
        sku = str(conn.execute("SELECT sku FROM products WHERE id = %s", [product]).fetchone()[0])  # type: ignore[index]
        first = f.version(
            conn,
            tenant,
            listing,
            content_json={"attributes": {"size": v}},
            category_template_id=template[0],
        )
        live = f.version(
            conn,
            tenant,
            listing,
            content_json={"attributes": {"size": w}},
            category_template_id=template[0],
        )
        conn.execute(
            "UPDATE listings SET published_version_id = %s, current_version_id = %s, "
            "status = 'published', first_published_at = now(), marketplace_state = 'processing' "
            "WHERE id = %s",
            [live, live, listing],
        )
        f.listing_issue(
            conn,
            tenant,
            listing,
            source="amazon_adapter",
            field_path="attributes.size",
            error_code="VALUE_NOT_ALLOWED",
            error_class="invalid_value",
            retry_class="user_action",
            message=f"The value {v} for {sku} is not allowed.",
            marketplace_code="99300",
            listing_version_id=first,
            status="resolved",
            resolution="marketplace_cleared",
            resolved_at="2026-11-01T00:00:00Z",
            dedupe_key=f"amazon_adapter|attributes.size|VALUE_NOT_ALLOWED|99300|{uuid.uuid4()}",
        )
    return listing, sku


def test_three_listings_on_two_tenants_make_one_line(
    gen_world: GenWorld, capsys: pytest.CaptureFixture[str]
) -> None:
    made: list[Any] = [
        _refused_then_live(gen_world, "acme", "XX-L", "XXL"),
        _refused_then_live(gen_world, "acme", "xx-l", "XXL"),
        _refused_then_live(gen_world, "bharat", "XX-L", "XXL"),
    ]
    assert main(["errors", "value-maps"]) == 0
    out = capsys.readouterr().out
    (line,) = [json.loads(x) for x in out.splitlines() if '"alias"' in x]
    assert (line["key"], line["w"], line["listings"], line["tenants"]) == ("size", "XXL", 3, 2)
    assert line["alias"] == '"xx-l": ("XXL",),'
    for listing, sku in made:
        assert str(listing) not in out and sku not in out
    for slug in ("acme", "bharat"):
        assert str(gen_world.tenant(slug)) not in out
    assert "not allowed" not in out
