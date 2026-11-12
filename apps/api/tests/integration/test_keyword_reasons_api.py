"""Client feedback 11 (bullet 6): the listing read carries `keyword_reasons`, and a seller edit keeps
the reasons of the terms still listed, drops the rest, and refreshes `used_in` on the new copy."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from psycopg.types.json import Jsonb

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world
from .test_listings_content_patch_api import ATTRS, CLEAN, _headers, _seeded

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]

REASONS = [
    {
        "term": "cotton kurta set",
        "reason": "Names the fabric and the product type.",
        "source": "product_facts",
        "fact_keys": ["attributes.fabric"],
        "used_in": ["backend"],
    },
    {
        "term": "straight kurta",
        "reason": "How shoppers ask for the cut.",
        "source": "search_intent",
        "fact_keys": [],
        "used_in": ["description", "backend"],
    },
]


def _with_reasons(world: GenWorld, version: uuid.UUID) -> None:
    """Set-up only (guards off): the current version as a v5 pipeline would have written it."""
    keywords = {"search_terms": ["cotton kurta set", "straight kurta"], "hidden_terms": []}
    with world.conn.transaction():
        world.conn.execute("SET LOCAL session_replication_role = replica")
        world.conn.execute(
            # the demo v1 stores bare ListingContent: wrap it as a full listing first
            "UPDATE listing_versions SET content_json = (CASE WHEN content_json ? 'content' "
            "THEN content_json ELSE jsonb_build_object('content', content_json) END) "
            "|| jsonb_build_object('keywords', %s::jsonb, 'keyword_reasons', %s::jsonb) "
            "WHERE id = %s",
            [Jsonb(keywords), Jsonb(REASONS), version],
        )


async def test_the_listing_read_returns_the_keyword_reasons(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    listing_id, version = _seeded(gen_world)
    _with_reasons(gen_world, version)
    response = await api.get(
        f"/api/v1/listings/{listing_id}", headers=gen_world.headers("operator@acme.test")
    )
    assert response.status_code == 200, response.text
    assert response.json()["content"]["keyword_reasons"] == REASONS


async def test_an_edit_keeps_listed_reasons_and_refreshes_where_they_are_used(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    listing_id, version = _seeded(gen_world)
    _with_reasons(gen_world, version)
    body: dict[str, Any] = {
        "content": CLEAN,
        "keywords": {"search_terms": ["cotton kurta set", "kurta for women"], "hidden_terms": []},
        "attributes": ATTRS,
    }
    response = await api.patch(
        f"/api/v1/listings/{listing_id}/content", headers=_headers(gen_world, version), json=body
    )
    assert response.status_code == 200, response.text
    reasons = response.json()["content"]["keyword_reasons"]
    # "straight kurta" left the list; the seller's new "kurta for women" has no reason
    assert [item["term"] for item in reasons] == ["cotton kurta set"]
    assert reasons[0]["reason"] == REASONS[0]["reason"]
    assert reasons[0]["fact_keys"] == ["attributes.fabric"]
    assert reasons[0]["used_in"] == ["title", "description", "backend"]
    stored = gen_world.conn.execute(
        "SELECT content_json FROM listing_versions WHERE id = %s",
        [gen_world.listing(listing_id)["current_version_id"]],
    ).fetchone()
    assert stored is not None
    assert [item["term"] for item in stored[0]["keyword_reasons"]] == ["cotton kurta set"]
