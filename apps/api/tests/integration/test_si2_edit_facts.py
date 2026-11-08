"""SI2 T17 (D5, CP-D7; AC7): a human edit of a factual listing attribute is refused (422
VALIDATION_FAILED at `attributes.<key>`); the seller's facts are edited on the product."""

from __future__ import annotations

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world
from .test_listings_content_patch_api import ATTRS, CLEAN, _headers, _patch, _seeded

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]


def _versions(world: GenWorld, listing_id: object) -> int:
    return world.count("listing_versions", "listing_id = %s", [listing_id])


@pytest.mark.parametrize(
    ("attributes", "field"),
    [
        ({"fabric_type": "Silk"}, "attributes.fabric_type"),
        ({"mrp": "10"}, "attributes.mrp"),
        ({"color": "blue", "gsm": "180"}, "attributes.color"),
    ],
)
async def test_factual_edit_refused(
    api: httpx.AsyncClient, gen_world: GenWorld, attributes: dict[str, str], field: str
) -> None:
    listing_id, version = _seeded(gen_world)
    response = await _patch(
        api, gen_world, listing_id, version, {"content": CLEAN, "attributes": attributes}
    )
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["type"] == "VALIDATION_FAILED"
    (detail,) = error["details"]
    assert ".".join(detail["loc"][1:]) == field
    assert "Silk" not in response.text and '"10"' not in response.text  # never echoed
    assert _versions(gen_world, listing_id) == 1


async def test_non_factual_edit_and_owner_allowed(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    listing_id, version = _seeded(gen_world)
    response = await _patch(api, gen_world, listing_id, version)  # pattern + neckline
    assert response.status_code == 200, response.text
    new = response.json()["current_version_id"]
    # resending the same attributes (no factual change) is fine
    again = await _patch(
        api,
        gen_world,
        listing_id,
        new,
        {"content": CLEAN, "attributes": ATTRS},
        headers=_headers(gen_world, new, "owner@acme.test"),
    )
    assert again.status_code == 200, again.text
    owner = await _patch(
        api,
        gen_world,
        listing_id,
        new,
        {"content": CLEAN, "attributes": {"fabric_type": "Silk"}},
        headers=_headers(gen_world, new, "owner@acme.test"),
    )
    assert owner.status_code == 422


def test_declared_code() -> None:
    from app.api.v1.surface import OPS, _error_codes
    from mkos_schemas.codes import ErrorCode

    assert ErrorCode.VALIDATION_FAILED in _error_codes(OPS["listings_content_patch"])[422]
