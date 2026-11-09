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


def run(data: bytes, marketplace: str, slot: ImageSlot = ImageSlot.MAIN) -> ImageReport:
    return check_image(
        data,
        image_id=IMAGE_ID,
        marketplace=Marketplace(marketplace),
        slot=slot,
        rules=seed_image_rules(marketplace),
        now=NOW,
    )


def codes(report: ImageReport) -> list[str]:
    return [i.code.value for i in report.issues]


@pytest.mark.parametrize("category", ["kurtas", "home_decor_lamps", "anything_else"])
def test_rules_seed_images_resolve_for_every_category(category: str) -> None:
    # QA 9 Nov: secondary and lifestyle resolution rows; SI14: the text-overlay and watermark
    # warnings (run only while PHOTO_QA_ENABLED is on)
    # photo rules 2.1.0: Amazon's main zoom warning and 3:4 and 10:13 shapes; Flipkart's
    # white-background row retired
    assert len(seed_image_rules("amazon", category)) == 15
    assert len(seed_image_rules("flipkart", category)) == 12


@pytest.mark.parametrize(
    ("case", "data", "marketplace", "passed", "expected"),
    [
        ("amazon_800_white_jpeg", jpeg(800, 800), "amazon", True, ["RESOLUTION_LOW"]),
        ("flipkart_800_white_jpeg", jpeg(800, 800), "flipkart", True, ["RESOLUTION_LOW"]),
        ("flipkart_1000x1333_3x4", jpeg(1000, 1333), "flipkart", True, []),
        ("amazon_1000x1333_3x4", jpeg(1000, 1333), "amazon", True, []),
        ("amazon_1000x1300_10x13", jpeg(1000, 1300), "amazon", True, []),
        ("amazon_1600x900_wide", jpeg(1600, 900), "amazon", True, ["WRONG_ASPECT"]),
        ("amazon_400_white", jpeg(400, 400), "amazon", False, ["RESOLUTION_LOW"]),
        ("amazon_1000_white", jpeg(1000, 1000), "amazon", True, []),
    ],
)
def test_rules_seed_images_golden(
    case: str, data: bytes, marketplace: str, passed: bool, expected: list[str]
) -> None:
    report = run(data, marketplace)
    assert report.pass_ is passed, case
    assert codes(report) == expected, case


def test_rules_seed_grey_background_blocks_on_amazon_only() -> None:
    """Amazon's main photo must have a pure white background (clothing style guide); Flipkart's
    white-background row is retired (apparel there is shot on light grey)."""
    amazon = run(jpeg(1000, 1000, border=GREY), "amazon")
    assert (amazon.pass_, codes(amazon)) == (False, ["BACKGROUND_OFF"])
    assert [i.severity.value for i in amazon.issues if i.severity] == ["error"]
    flipkart = run(jpeg(1000, 1000, border=GREY), "flipkart")
    assert (flipkart.pass_, codes(flipkart)) == (True, [])


def test_rules_seed_demo_tshirt_photo_fails_only_the_background() -> None:
    """Prod demo 9 Nov: an 840x1120 JPEG on light grey failed BACKGROUND_OFF, RESOLUTION_LOW and
    WRONG_ASPECT. Amazon measures size on the longest side (1120 px: zoom works) and asks no
    fixed shape (3:4 is a clothing shape), so only the background is left, and it blocks."""
    report = run(jpeg(840, 1120, border=(235, 235, 235)), "amazon")
    assert codes(report) == ["BACKGROUND_OFF"]
    assert report.pass_ is False
    white = run(jpeg(840, 1120), "amazon")
    assert (white.pass_, codes(white)) == (True, [])


def test_rules_seed_each_issue_keeps_its_rule_severity() -> None:
    """A 600x800 photo on grey: the zoom warning stays a warning beside the blocking background."""
    report = run(jpeg(600, 800, border=GREY), "amazon")
    assert {i.code.value: i.severity.value for i in report.issues if i.severity} == {
        "RESOLUTION_LOW": "warning",
        "BACKGROUND_OFF": "error",
    }


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


@pytest.mark.parametrize("marketplace", ["amazon", "flipkart"])
def test_rules_seed_photo_qa_rows_are_warnings(marketplace: str) -> None:
    """SI14 (A7): a text-overlay and a watermark row per marketplace, main slot, warnings."""
    rows = [
        r
        for r in seed_image_rules(marketplace)
        if r.rule_def.check in ("text_overlay", "watermark")  # type: ignore[union-attr]
    ]
    assert sorted(r.rule_def.check for r in rows) == ["text_overlay", "watermark"]  # type: ignore[union-attr]
    assert {r.severity.value for r in rows} == {"warning"}
    assert {r.rule_def.slot for r in rows} == {"main"}  # type: ignore[union-attr]
    assert all(r.category is None for r in rows)


@pytest.mark.parametrize("slot", [ImageSlot.SECONDARY, ImageSlot.LIFESTYLE])
@pytest.mark.parametrize("marketplace", ["amazon", "flipkart"])
def test_rules_seed_small_extra_photo_fails_resolution(marketplace: str, slot: ImageSlot) -> None:
    """QA: a 200x150 Back, Close-up or More photo failed nothing; the floor is 500 px."""
    report = run(jpeg(200, 150), marketplace, slot)
    assert report.pass_ is False
    assert codes(report) == ["RESOLUTION_LOW"]
    assert "resolution" in {c.check for c in report.checks}


@pytest.mark.parametrize("slot", [ImageSlot.SECONDARY, ImageSlot.LIFESTYLE])
@pytest.mark.parametrize("marketplace", ["amazon", "flipkart"])
def test_rules_seed_extra_photo_resolution(marketplace: str, slot: ImageSlot) -> None:
    """800 px passes with a zoom warning; 1000 px passes clean; no 1:1 rule on extra photos."""
    mid = run(jpeg(800, 800), marketplace, slot)
    assert (mid.pass_, codes(mid)) == (True, ["RESOLUTION_LOW"])
    big = run(jpeg(1000, 1333), marketplace, slot)
    assert (big.pass_, codes(big)) == (True, [])
