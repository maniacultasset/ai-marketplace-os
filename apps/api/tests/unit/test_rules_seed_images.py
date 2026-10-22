"""S17b T5 (PLAN D12; AC10): the seed's image rows, resolved as the job resolves them, give the
golden outcomes on generated images (no committed binary)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from app.cli import RULES_SEED_PATH
from app.db.platform_loads import load_rule_sets
from app.services.image_checks import check_image
from app.services.rules_engine import RuleRow, resolve_ruleset
from mkos_schemas.enums import ImageSlot, Marketplace
from mkos_schemas.images import ImageReport
from mkos_schemas.rules import Rule
from tests.image_fixtures import IMAGE_ID, NOW, jpeg

GREY = (200, 200, 200)


def seed_image_rules(marketplace: str, category: str = "kurtas") -> list[Rule]:
    """The seed's image rows for `marketplace`, through the resolver for any category (they are
    category-NULL platform defaults)."""
    market = Marketplace(marketplace)
    rows = [
        RuleRow(rule=r, revision=1)
        for s in load_rule_sets(RULES_SEED_PATH)
        for r in s.rules
        if r.field == "images" and r.marketplace is market
    ]
    ruleset = resolve_ruleset(
        rows, marketplace=market, category=category, tenant_id=None, loaded_at=datetime.now(UTC)
    )
    return [r for r in ruleset.rules if r.field == "images"]


def run(data: bytes, marketplace: str) -> ImageReport:
    return check_image(
        data,
        image_id=IMAGE_ID,
        marketplace=Marketplace(marketplace),
        slot=ImageSlot.MAIN,
        rules=seed_image_rules(marketplace),
        now=NOW,
    )


def codes(report: ImageReport) -> list[str]:
    return [i.code.value for i in report.issues]


@pytest.mark.parametrize("category", ["kurtas", "home_decor_lamps", "anything_else"])
def test_rules_seed_images_resolve_for_every_category(category: str) -> None:
    assert len(seed_image_rules("amazon", category)) == 6
    assert len(seed_image_rules("flipkart", category)) == 7


@pytest.mark.parametrize(
    ("case", "data", "marketplace", "passed", "expected"),
    [
        ("amazon_800_white_jpeg", jpeg(800, 800), "amazon", False, ["RESOLUTION_LOW"]),
        ("flipkart_800_white_jpeg", jpeg(800, 800), "flipkart", True, ["RESOLUTION_LOW"]),
        ("flipkart_1000x1333_3x4", jpeg(1000, 1333), "flipkart", True, []),
        ("amazon_1000x1333_3x4", jpeg(1000, 1333), "amazon", False, ["WRONG_ASPECT"]),
        ("amazon_1000_white", jpeg(1000, 1000), "amazon", True, []),
    ],
)
def test_rules_seed_images_golden(
    case: str, data: bytes, marketplace: str, passed: bool, expected: list[str]
) -> None:
    report = run(data, marketplace)
    assert report.pass_ is passed, case
    assert codes(report) == expected, case


@pytest.mark.parametrize("marketplace", ["amazon", "flipkart"])
def test_rules_seed_grey_background_warning_only(marketplace: str) -> None:
    report = run(jpeg(1000, 1000, border=GREY), marketplace)
    assert report.pass_ is True
    assert codes(report) == ["BACKGROUND_OFF"]


@pytest.mark.parametrize("marketplace", ["amazon", "flipkart"])
def test_rules_seed_images_vision_rows_not_listed(marketplace: str) -> None:
    report = run(jpeg(1000, 1000), marketplace)
    assert {c.check for c in report.checks} & {
        "fill_ratio",
        "text_overlay",
        "watermark",
        "logo",
    } == set()
    assert all(c.method == "deterministic" for c in report.checks)
