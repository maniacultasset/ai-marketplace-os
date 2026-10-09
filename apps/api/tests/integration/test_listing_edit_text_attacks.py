"""S18 re-check (tester): raw-JSON text attacks on the content PATCH, regenerate and generate
routes. A lone surrogate, a non-finite number, or a control character must answer 422 with
nothing written, never a 500."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world
from .test_listings_content_patch_api import ATTRS, CLEAN, _headers, _seeded

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]

# Raw JSON fragments: the body is sent as text so that `\ud800` and `NaN` reach the server as is.
_TITLE = '{"content": {"title": %s, "bullets": ["Soft cotton fabric"], "description": "A d."}}'
_ATTR = (
    '{"content": {"title": "T", "bullets": ["b"], "description": "d"}, '
    '"attributes": {"color": "blue", "fabric_type": "cotton", "weight": %s}}'
)
RAW = {
    "lone_surrogate_title": _TITLE % '"a\\ud800b"',
    "nul_escape_title": _TITLE % '"a\\u0000b"',
    "nan_attr": _ATTR % "NaN",
    "inf_attr": _ATTR % "Infinity",
    "neg_inf_attr": _ATTR % "-Infinity",
    "huge_number_attr": _ATTR % "1e400",
    "surrogate_attr_key": (
        '{"content": {"title": "T", "bullets": ["b"], "description": "d"}, '
        '"attributes": {"color": "blue", "fabric_type": "cotton", "k\\udfff": "v"}}'
    ),
}


async def _raw_patch(
    api: httpx.AsyncClient, world: GenWorld, listing_id: Any, version: Any, raw: str
) -> httpx.Response:
    headers = {**_headers(world, version), "Content-Type": "application/json"}
    return await api.patch(
        f"/api/v1/listings/{listing_id}/content", headers=headers, content=raw.encode()
    )


@pytest.mark.parametrize("case", list(RAW))
async def test_patch_raw_text_attacks_422(
    api: httpx.AsyncClient, gen_world: GenWorld, case: str
) -> None:
    listing_id, version = _seeded(gen_world)
    response = await _raw_patch(api, gen_world, listing_id, version, RAW[case])
    assert response.status_code == 422, response.text
    assert response.json()["error"]["type"] == "VALIDATION_FAILED"
    assert gen_world.count("listing_versions", "listing_id = %s", [listing_id]) == 1
    assert gen_world.listing(listing_id)["current_version_id"] == version


async def test_regenerate_lone_surrogate_instructions_422(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    listing_id, _version = _seeded(gen_world)
    headers = {**gen_world.headers("operator@acme.test"), "Content-Type": "application/json"}
    response = await api.post(
        f"/api/v1/listings/{listing_id}/regenerate",
        headers=headers,
        content=b'{"instructions": "a\\ud800b"}',
    )
    assert response.status_code == 422, response.text
    assert gen_world.listing(listing_id)["status"] == "validated"


@pytest.mark.parametrize("bad", ["a\x00b", "a\x07b", "a\x1bb"], ids=repr)
async def test_generate_control_char_instructions_422(
    api: httpx.AsyncClient, gen_world: GenWorld, bad: str
) -> None:
    product = gen_world.product("acme", "KURTA-001")
    tenant = gen_world.tenant("acme")
    jobs_before = gen_world.count("job_runs", "tenant_id = %s", [tenant])
    response = await api.post(
        "/api/v1/listings/generate",
        headers=gen_world.headers("operator@acme.test"),
        json={
            "product_id": str(product),
            "marketplace": "amazon",
            "user_instructions": bad,
            "force_regenerate": True,
        },
    )
    assert response.status_code == 422, response.text
    assert gen_world.count("job_runs", "tenant_id = %s", [tenant]) == jobs_before


async def test_patch_body_with_c1_and_bidi_is_stored_verbatim(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    """Only C0 (and DEL) is refused: a C1 control or a bidi mark is ordinary text (no 500)."""
    listing_id, version = _seeded(gen_world)
    text = (
        "A cotton kurta "
        + chr(0x85)
        + " set "
        + chr(0x202E)
        + " with a fit "
        + chr(0x2028)
        + " here."
    )
    headers = _headers(gen_world, version)
    response = await api.patch(
        f"/api/v1/listings/{listing_id}/content",
        headers=headers,
        json={"content": {**CLEAN, "description": text}, "attributes": ATTRS},
    )
    assert response.status_code == 200, response.text
    assert response.json()["content"]["content"]["description"] == text
