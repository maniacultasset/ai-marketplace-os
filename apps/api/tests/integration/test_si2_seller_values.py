"""SI2 T16 (AC7, AC8; D5, D15): the seller's factual values win at generation, in the stored
version and in the re-check; an AI factual value the seller never gave is dropped."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import httpx
import pytest

from . import factories as f
from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world, make_generating
from .review_support import OPERATOR, run_job

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]
CATEGORY = "ethnic_wear_kurta_sets"


async def _product(world: GenWorld, attributes: dict[str, Any]) -> str:
    from app.services.products import NewProduct, create_product

    sku = f"SI2V-{uuid.uuid4().hex[:8].upper()}"
    await create_product(
        world.principal(OPERATOR),
        NewProduct(
            sku=sku,
            title="Cotton Kurta Set",
            category=CATEGORY,
            images=(),
            attributes=attributes,
            mrp=Decimal("1499.00"),
            selling_price=Decimal("999.00"),
            # SI5 B2 + SI9 B4: the pre-approval check needs a barcode for an Amazon create
            product_identifier=("ean", f.ean()),
            gender="women",  # SI8 + SI9 B4: and a Kurta set needs the audience
        ),
    )
    return sku


def _ai(monkeypatch: pytest.MonkeyPatch, attributes: dict[str, Any], score: float) -> None:
    """The stub pipeline, with the AI's factual guesses written over its listing."""
    from app.workers import generation
    from mkos_pipeline import run_listing_pipeline as real

    async def guessing(ctx: Any, deps: Any) -> Any:
        result = await real(ctx, deps)
        if result.listing is None:
            return result
        listing = result.listing.model_copy(
            update={"attributes": attributes, "confidence_score": score}
        )
        return result.model_copy(update={"listing": listing})

    monkeypatch.setattr(generation, "run_listing_pipeline", guessing)


async def _generate(world: GenWorld, sku: str) -> tuple[uuid.UUID, dict[str, Any]]:
    accepted = await make_generating(world, sku=sku)
    await run_job(accepted.job_id)
    row = world.listing(accepted.listing_id)
    assert row["current_version_id"] is not None, row
    version = world.conn.execute(
        "SELECT content_json FROM listing_versions WHERE id = %s", [row["current_version_id"]]
    ).fetchone()
    assert version is not None
    return accepted.listing_id, version[0]


def _run_errors(world: GenWorld, listing_id: uuid.UUID) -> list[dict[str, Any]]:
    row = world.conn.execute(
        "SELECT error_report_json FROM validation_runs WHERE listing_id = %s AND source = "
        "'rules_engine' ORDER BY created_at DESC LIMIT 1",
        [listing_id],
    ).fetchone()
    assert row is not None
    return list(row[0].get("errors") or [])


async def test_seller_values_beat_the_ai(
    api: httpx.AsyncClient, gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    sku = await _product(gen_world, {"fabric_type": "Cotton Lycra", "color": "Mustard"})
    _ai(
        monkeypatch, {"fabric_type": "Silk", "color": "Navy", "gsm": "180", "pattern": "solid"}, 0.9
    )
    listing_id, content = await _generate(gen_world, sku)
    attributes = content["attributes"]
    assert attributes["fabric_type"] == "Cotton Lycra" and attributes["color"] == "Mustard"
    assert "gsm" not in attributes and attributes["pattern"] == "solid"
    fields = {e["field"] for e in _run_errors(gen_world, listing_id)}
    assert "attributes.fabric_type" not in fields and "attributes.color" not in fields
    preview = await api.get(
        f"/api/v1/listings/{listing_id}/preview", headers=gen_world.headers(OPERATOR)
    )
    attrs = preview.json()["items"][0]["payload"]["attributes"]
    assert attrs["material"][0]["value"] == "Cotton Lycra"
    assert attrs["color"][0]["value"] == "Mustard"


async def test_no_seller_fabric_is_missing_and_blocks(
    gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    sku = await _product(gen_world, {"color": "Mustard"})
    _ai(monkeypatch, {"fabric_type": "Silk", "color": "Navy"}, 0.9)
    listing_id, content = await _generate(gen_world, sku)
    assert "fabric_type" not in content["attributes"]
    codes = {(e["field"], e["error_code"]) for e in _run_errors(gen_world, listing_id)}
    assert ("attributes.fabric_type", "MISSING_ATTRIBUTE") in codes
    assert gen_world.listing(listing_id)["status"] == "blocked"


@pytest.mark.parametrize(("score", "kept"), [(0.80, True), (0.50, False)])
async def test_ai_colour_needs_the_dictionary_confidence(
    gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch, score: float, kept: bool
) -> None:
    sku = await _product(gen_world, {"fabric_type": "Cotton"})
    _ai(monkeypatch, {"color": "Navy"}, score)
    listing_id, content = await _generate(gen_world, sku)
    assert ("color" in content["attributes"]) is kept
    # the template requires a colour: without one the stored re-check blocks the listing
    assert (gen_world.listing(listing_id)["status"] == "blocked") is not kept


async def test_unmappable_seller_colour_is_refused_before_approval(
    gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    sku = await _product(gen_world, {"fabric_type": "Cotton", "color": "Neon Plaid"})
    _ai(monkeypatch, {"color": "Navy"}, 0.9)
    listing_id, _ = await _generate(gen_world, sku)
    codes = {(e["field"], e["error_code"]) for e in _run_errors(gen_world, listing_id)}
    assert ("attributes.color", "VALUE_NOT_ALLOWED") in codes
