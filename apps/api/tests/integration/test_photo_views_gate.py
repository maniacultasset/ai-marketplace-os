"""Feedback 7 and 8 (`PHOTO_VIEWS_ENABLED`): the photo view checklist and the size chart check at
approval, in the product read and in `GET /catalog/requirements`, as app_rw.

The Kurta set checklist requires the front photo and the seller's size chart, and recommends a
back and a close-up photo. A missing required view refuses the approve item 422
`IMAGE_NON_COMPLIANT` with the view findings; recommended views only warn; a variant size the
saved chart lacks blocks. Off, nothing changes (the S17b gate tests cover that path)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from mkos_schemas.enums import ImageSlot, Marketplace
from mkos_schemas.images import ImageCheck, ImageReport

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world
from .images_support import image_row
from .review_support import REVIEWER, Made, generated, item, only_item, post_approve
from .test_approve_image_gate import _store, _with_images

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]
WHEN = datetime(2026, 11, 11, tzinfo=UTC)
CHART = {"unit": "cm", "basis": "garment", "rows": [{"size": "M", "chest": 100.0}]}


@pytest.fixture(autouse=True)
def _on(monkeypatch: pytest.MonkeyPatch) -> Any:
    from app.core.config import get_settings

    monkeypatch.setenv("PHOTO_VIEWS_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _passed(image_id: uuid.UUID, slot: ImageSlot = ImageSlot.MAIN) -> ImageReport:
    return ImageReport(
        image_id=image_id,
        marketplace=Marketplace.AMAZON,
        slot=slot,
        pass_=True,
        checks=[ImageCheck(check="format", method="deterministic", passed=True)],
        issues=[],
        processed_at=WHEN,
    )


def _product(world: GenWorld, made: Made) -> uuid.UUID:
    product: uuid.UUID = world.listing(made.listing_id)["product_id"]
    return product


async def _main(world: GenWorld, made: Made) -> uuid.UUID:
    image = image_row(world.conn, world.tenant("acme"), _product(world, made), slot="main")
    await _store(world, "acme", image, _passed(image))
    return image


def _save_chart(world: GenWorld, product: uuid.UUID) -> None:
    from app.services.product_facts import size_chart_fact

    fact = size_chart_fact(CHART, now=WHEN)
    world.conn.execute(
        "UPDATE products SET attributes = attributes || jsonb_build_object("
        "'size_measurements', %s::jsonb) WHERE id = %s",
        [json.dumps(fact), product],
    )


def _codes(result: dict[str, Any]) -> dict[str, str]:
    return {e["raw_code"]: e["severity"] for e in result["error"]["errors"] if e["raw_code"]}


async def test_missing_required_views_block_approve(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world)
    result = only_item(await post_approve(api, gen_world, REVIEWER, [item(made)]))
    assert (result["http_status"], result["error"]["error_code"]) == (422, "IMAGE_NON_COMPLIANT")
    assert _codes(result) == {
        "photo_view:front": "error",
        "photo_view:back": "warning",
        "photo_view:close_up": "warning",
        "photo_view:size_chart": "error",
    }
    row = gen_world.listing(made.listing_id)
    assert (row["status"], row["approved_version_id"]) == ("validated", None)


async def test_front_and_saved_chart_approve_with_warnings_only(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world)
    made = _with_images(gen_world, made, [await _main(gen_world, made)])
    _save_chart(gen_world, _product(gen_world, made))
    result = only_item(await post_approve(api, gen_world, REVIEWER, [item(made)]))
    assert result["http_status"] == 200, result
    assert gen_world.listing(made.listing_id)["status"] == "approved"


async def test_product_read_lists_views_and_the_size_chart_check(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world)
    image = await _main(gen_world, made)
    product = _product(gen_world, made)
    response = await api.get(f"/api/v1/products/{product}", headers=gen_world.headers(REVIEWER))
    assert response.status_code == 200, response.text
    body = response.json()
    views = {v["view"]: v for v in body["photo_views"]}
    assert list(views)[:4] == ["front", "back", "close_up", "size_chart"]
    assert views["front"]["status"] == "present"
    assert views["front"]["image_ids"] == [str(image)]
    assert views["front"]["slot"] == "main"
    assert (views["back"]["slot"], views["back"]["role"]) == ("secondary", "back")
    assert views["size_chart"]["requirement"] == "required"
    assert views["size_chart"]["status"] == "missing"
    assert body["size_chart_check"] == {
        "required": True,
        "saved": False,
        "missing_sizes": [],
        "unused_sizes": [],
    }
    _save_chart(gen_world, product)
    response = await api.get(f"/api/v1/products/{product}", headers=gen_world.headers(REVIEWER))
    body = response.json()
    assert {v["view"]: v["status"] for v in body["photo_views"]}["size_chart"] == "present"
    assert body["size_chart_check"]["saved"] is True


async def test_requirements_name_the_size_chart_and_views(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    response = await api.get(
        "/api/v1/catalog/requirements",
        params={"category": "ethnic_wear_kurta_sets"},
        headers=gen_world.headers(REVIEWER),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert "size_measurements" in body["amazon"]
    assert "size_measurements" in body["flipkart"]
    rows = {v["view"]: v for v in body["photo_views"]}
    assert rows["front"]["requirement"] == "required"
    assert rows["back"]["amazon"] == rows["back"]["flipkart"] == "recommended"
    assert rows["size_chart"]["why"]
