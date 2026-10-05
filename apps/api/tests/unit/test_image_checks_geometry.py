"""Resolution and aspect ratio, measured on the EXIF-oriented size."""

from __future__ import annotations

import io
import tokenize
from pathlib import Path

from app.services import image_checks
from mkos_schemas.rules import ImageRequirement, Rule
from tests import image_fixtures as fx
from tests.image_fixtures import codes, image_rule, names, run


def amazon_main() -> list[Rule]:
    return [
        image_rule("min_resolution", min_px=1000),
        image_rule("aspect_ratio", ratio_w=1, ratio_h=1),
    ]


def flipkart_main() -> list[Rule]:
    return [
        image_rule("min_resolution", marketplace="flipkart", min_px=500),
        image_rule(
            "recommended_resolution", marketplace="flipkart", severity="warning", min_px=1000
        ),
        image_rule("aspect_ratio", marketplace="flipkart", ratio_w=1, ratio_h=1),
        image_rule("aspect_ratio", marketplace="flipkart", pack="alt_ratio", ratio_w=3, ratio_h=4),
    ]


def _check(report_checks: list[tuple[str, bool]], name: str) -> bool:
    return dict(report_checks)[name]


# 1, 2. boundaries


def test_pillow_checks_resolution_pass() -> None:
    report = run(fx.jpeg(1000, 1000), [image_rule("min_resolution", min_px=1000)])
    assert report.pass_ is True and names(report)[-1] == "resolution"


def test_pillow_checks_resolution_fail() -> None:
    report = run(fx.jpeg(999, 1000), [image_rule("min_resolution", min_px=1000)])
    assert codes(report) == ["RESOLUTION_LOW"] and report.pass_ is False


def test_pillow_checks_aspect_ratio_pass() -> None:
    report = run(fx.jpeg(1010, 1000), [image_rule("aspect_ratio", ratio_w=1, ratio_h=1)])
    assert report.pass_ is True and names(report)[-1] == "aspect_ratio"


def test_pillow_checks_aspect_ratio_fail() -> None:
    report = run(fx.jpeg(1011, 1000), [image_rule("aspect_ratio", ratio_w=1, ratio_h=1)])
    assert codes(report) == ["WRONG_ASPECT"] and report.pass_ is False


def test_aspect_ratio_portrait_boundary() -> None:
    rule = [image_rule("aspect_ratio", ratio_w=1, ratio_h=1)]
    assert run(fx.jpeg(1000, 1010), rule).pass_ is True
    assert run(fx.jpeg(1000, 1011), rule).pass_ is False


# 3, 4. thresholds and severity come from rules


def test_thresholds_come_from_rules() -> None:
    data = fx.jpeg(800, 800)
    amazon = run(data, amazon_main())
    assert amazon.pass_ is False and codes(amazon) == ["RESOLUTION_LOW"]
    flipkart = run(data, flipkart_main(), marketplace="flipkart")
    assert flipkart.pass_ is True and codes(flipkart) == ["RESOLUTION_LOW"]


def test_image_warning_does_not_fail() -> None:
    rule = image_rule("recommended_resolution", severity="warning", min_px=1000)
    report = run(fx.jpeg(800, 800), [rule])
    assert report.pass_ is True
    assert [(c.check, c.passed) for c in report.checks][-1] == ("resolution", False)
    assert codes(report) == ["RESOLUTION_LOW"]


# 5. alternatives


def test_aspect_ratio_alternatives() -> None:
    rows = [r for r in flipkart_main() if isinstance(r.rule_def, ImageRequirement)]
    aspect = [r for r in rows if r.rule_def.check == "aspect_ratio"]  # type: ignore[union-attr]  # filtered above
    keys = {
        (r.field, r.rule_def.type, f"{r.rule_def.slot}:{r.rule_def.check}", r.rule_def.pack, r.version)  # type: ignore[union-attr]  # filtered above
        for r in aspect
    }
    assert len(keys) == len(aspect) == 2
    portrait = run(fx.jpeg(900, 1200), aspect, marketplace="flipkart")
    assert portrait.pass_ is True
    landscape = run(fx.jpeg(1000, 800), aspect, marketplace="flipkart")
    assert codes(landscape) == ["WRONG_ASPECT"]
    assert names(landscape).count("aspect_ratio") == 1


def test_aspect_ratio_any_and_main_both_apply() -> None:
    rules = [
        image_rule("aspect_ratio", slot="any", ratio_w=1, ratio_h=1),
        image_rule("aspect_ratio", slot="main", ratio_w=3, ratio_h=4),
    ]
    report = run(fx.jpeg(900, 1200), rules)
    assert codes(report) == ["WRONG_ASPECT"]
    assert [c.check for c in report.checks].count("aspect_ratio") == 1
    # different packs in different slots are still not alternatives: each slot group must pass
    split = [
        image_rule("aspect_ratio", slot="any", ratio_w=1, ratio_h=1),
        image_rule("aspect_ratio", slot="main", pack="alt_ratio", ratio_w=3, ratio_h=4),
    ]
    assert codes(run(fx.jpeg(900, 1200), split)) == ["WRONG_ASPECT"]


def test_aspect_ratio_same_pack_not_alternatives() -> None:
    rules = [
        image_rule("aspect_ratio", ratio_w=1, ratio_h=1),
        image_rule("aspect_ratio", ratio_w=3, ratio_h=4),
    ]
    assert codes(run(fx.jpeg(900, 1200), rules)) == ["WRONG_ASPECT"]


def test_aspect_ratio_group_fails_at_most_severe() -> None:
    rules = [
        image_rule("aspect_ratio", severity="warning", suggestion="warn", ratio_w=1, ratio_h=1),
        image_rule("aspect_ratio", pack="alt", suggestion="err", ratio_w=3, ratio_h=4),
    ]
    report = run(fx.jpeg(1000, 800), rules)
    assert report.pass_ is False and [i.suggestion for i in report.issues] == ["err"]


# 6. EXIF orientation


def test_exif_orientation_resolution_and_aspect() -> None:
    rules = [
        image_rule("aspect_ratio", ratio_w=3, ratio_h=4),
        image_rule("min_resolution", min_px=1000),
    ]
    stored = fx.jpeg(1600, 1200, exif_orientation=6)
    assert run(stored, rules).pass_ is True
    assert codes(run(fx.jpeg(1600, 1200), rules)) == ["WRONG_ASPECT"]


# 7. max_px and no early stop after decode


def test_max_px_fails_resolution() -> None:
    report = run(fx.jpeg(1200, 900), [image_rule("min_resolution", max_px=1000)])
    assert codes(report) == ["RESOLUTION_LOW"]
    assert report.issues[0].suggestion == "Downscale and re-export"
    assert run(fx.jpeg(1000, 900), [image_rule("min_resolution", max_px=1000)]).pass_ is True


def test_post_decode_checks_all_run() -> None:
    rules = [
        image_rule("min_resolution", min_px=1000),
        image_rule("aspect_ratio", ratio_w=1, ratio_h=1),
    ]
    report = run(fx.jpeg(600, 300, border=(0, 0, 0)), rules)
    assert names(report) == [
        "format",
        "corruption",
        "exif_orientation",
        "resolution",
        "aspect_ratio",
    ]
    assert codes(report) == ["RESOLUTION_LOW", "WRONG_ASPECT"]


# 8. no marketplace numbers in the module


def test_no_marketplace_thresholds_in_module() -> None:
    source = Path(image_checks.__file__).read_text("utf-8")
    numbers = {
        tok.string.replace("_", "")
        for tok in tokenize.generate_tokens(io.StringIO(source).readline)
        if tok.type == tokenize.NUMBER
    }
    assert numbers.isdisjoint({"500", "1000", "1500", "0.85", "10485760"})
