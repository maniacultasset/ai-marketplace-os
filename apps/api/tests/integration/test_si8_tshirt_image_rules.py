"""SI8 T11 (#8; AC12): after `rules load`, the image rules `image_compliance` resolves for
`tshirts` on both marketplaces hold the seeded main-image rules (stored with a NULL category), and
the new Amazon text rules resolve for kurta sets and t-shirts. Run in the Docker batch."""

from __future__ import annotations

from typing import Any

import pytest
from app.cli import main

from .auth_support import World
from .conftest import LOCAL_HOSTS

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
# photo rules 2.1.0: the resolver keeps the newest version of each main rule (Amazon size, shape
# and background v2, fill v1; Flipkart size v1, shape v2; Flipkart's white background retired)
MAIN_RULES = {
    "amazon": {
        f"5eed0000-0000-4000-8000-{n}"
        for n in ("000701000101", "000701000102", "000701000103", "000701000004")
    },
    "flipkart": {
        f"5eed0000-0000-4000-8000-{n}"
        for n in ("000702000001", "000702000002", "000702000103", "000702000104")
    },
}


@pytest.fixture
def loaded(products_world: World, capsys: pytest.CaptureFixture[str]) -> World:
    """`rules load` (the migrate job's loader) from a sync fixture: it runs its own event loop."""
    assert main(["rules", "load"]) == 0
    capsys.readouterr()
    return products_world


async def _resolved(world: World, marketplace: str, category: str) -> Any:
    from app.repositories.uow import tenant_uow
    from app.services.rules_loader import load_rules

    tenant = world.tenant("acme")
    async with tenant_uow(tenant) as uow:
        loaded = await load_rules(uow, tenant_id=tenant, marketplace=marketplace, category=category)
    return loaded.ruleset


@pytest.mark.parametrize("marketplace", ["amazon", "flipkart"])
async def test_tshirt_image_rules_hold_the_main_rules(loaded: World, marketplace: str) -> None:
    ruleset = await _resolved(loaded, marketplace, "tshirts")
    images = {str(r.id) for r in ruleset.rules if r.field == "images"}
    assert MAIN_RULES[marketplace] <= images


@pytest.mark.parametrize("category", ["ethnic_wear_kurta_sets", "tshirts"])
async def test_amazon_text_rules_resolve(loaded: World, category: str) -> None:
    ruleset = await _resolved(loaded, "amazon", category)
    fields = {(r.field, r.rule_def.type) for r in ruleset.rules}
    assert {
        ("content.bullets", "max_items"),
        ("content.bullets[*]", "max_length"),
        ("content.description", "max_length"),
    } <= fields
