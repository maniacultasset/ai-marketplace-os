"""Golden fixture round-trips."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mkos_schemas.errors import ErrorReport
from mkos_schemas.export import MODELS
from mkos_schemas.jobs import GenerationResult
from mkos_schemas.rules import RuleSet
from pydantic import BaseModel

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SECRET_MARKERS = (
    "sk-",
    "ghp_",
    "AKIA",
    "Atza|",
    "Atzr|",
    "ASIA",
    "-----BEGIN ",
    "eyJ",
)


@pytest.mark.parametrize("model", MODELS, ids=lambda m: m.__name__)
def test_golden_roundtrip(model: type[BaseModel]) -> None:
    path = FIXTURES / f"{model.__name__}.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    obj = model.model_validate(raw)
    assert obj.model_dump(by_alias=True, mode="json") == raw


@pytest.mark.parametrize("model", MODELS, ids=lambda m: m.__name__)
def test_golden_roundtrip_json(model: type[BaseModel]) -> None:
    path = FIXTURES / f"{model.__name__}.json"
    text = path.read_text(encoding="utf-8")
    obj = model.model_validate_json(text)
    assert json.loads(obj.model_dump_json(by_alias=True)) == json.loads(text)


def test_every_model_has_a_fixture() -> None:
    for model in MODELS:
        assert (FIXTURES / f"{model.__name__}.json").is_file()


def golden_stems(directory: Path) -> set[str]:
    """Golden fixture stems; `*.seed.json` is seed data (S9 D10), not a golden fixture."""
    return {p.stem for p in directory.glob("*.json") if not p.stem.endswith(".seed")}


def test_no_orphan_fixtures() -> None:
    names = golden_stems(FIXTURES)
    expected = {m.__name__ for m in MODELS}
    assert names == expected


def test_every_module_with_models_is_covered() -> None:
    assert len(MODELS) == 20


def test_fixtures_hold_no_secrets() -> None:
    cred = json.loads((FIXTURES / "ModelCredentials.json").read_text())
    assert cred["api_key"] == "**********"
    for path in FIXTURES.glob("*.json"):
        body = path.read_text(encoding="utf-8")
        for marker in SECRET_MARKERS:
            assert marker not in body, f"{path.name} contains {marker!r}"


def test_fixtures_are_canonically_formatted_and_seeded() -> None:
    listing_path = FIXTURES / "CanonicalListing.json"
    listing = json.loads(listing_path.read_text(encoding="utf-8"))
    assert (
        listing_path.read_text(encoding="utf-8")
        == json.dumps(listing, indent=2, ensure_ascii=False) + "\n"
    )
    assert (
        listing["content"]["title"]
        == "Fabrica Organic Cotton Kurta Set for Women | Indigo | Handloom Weave"
    )
    err = json.loads((FIXTURES / "ErrorReport.json").read_text())
    assert err["errors"][0]["message"] == "Title is 221 chars; Amazon limit is 200"


def test_context_and_result_fixtures_are_one_pair() -> None:
    """The AI team builds tests from this pair, so it carries one category and one set of ids."""
    ctx = json.loads((FIXTURES / "GenerationContext.json").read_text(encoding="utf-8"))
    result = json.loads((FIXTURES / "GenerationResult.json").read_text(encoding="utf-8"))
    assert ctx["request_id"] == result["request_id"]
    assert ctx["job_id"] == result["job_id"]
    for key in ("product_id", "listing_id", "marketplace", "category"):
        assert ctx[key] == result["listing"][key], key
    assert ctx["marketplace_rules"]["category"] == ctx["category"]
    assert ctx["category_template"]["category"] == ctx["category"]
    # A key the seller just answered is not asked again as missing in the paired result.
    asked = {q["target_path"].removeprefix("facts.") for q in result["questions"]}
    assert not asked & set(ctx["answered_keys"])


def test_listing_fixture_claims_match_the_text() -> None:
    """A kept claim points at text that is there; a removed claim's sentence is gone (D1-20)."""
    listing = json.loads((FIXTURES / "CanonicalListing.json").read_text(encoding="utf-8"))
    bullets = listing["content"]["bullets"]
    statuses = {claim["status"] for claim in listing["claims"]}
    assert {"supported", "removed"} <= statuses
    for claim in listing["claims"]:
        path = claim["text_path"]
        if not path.startswith("content.bullets["):
            continue
        index = int(path.removeprefix("content.bullets[").removesuffix("]"))
        if claim["status"] == "removed":
            assert index >= len(bullets), path
            assert claim["fact_keys"] == []
        else:
            assert index < len(bullets), path
    # No certifications fact backs a certification claim (D2-21): none is kept in the text.
    assert not any("GOTS" in bullet for bullet in bullets)


def test_context_fixture_carries_the_label_image() -> None:
    ctx = json.loads((FIXTURES / "GenerationContext.json").read_text(encoding="utf-8"))
    slots = [image["slot"] for image in ctx["images"]]
    assert "packaging_label" in slots and "main" in slots


# ---- 1.2.0 is additive: what a 1.1.0 producer sends still parses (C14) ----
# golden_1_1_0/ holds the 1.1.0 fixtures as they were before the bump; never regenerate them.
GOLDEN_1_1_0 = Path(__file__).resolve().parent / "golden_1_1_0"
_NEW_IN_1_2_0: dict[str, dict[str, object]] = {
    "GenerationContext": {
        "product_provenance": {},
        "conflicts": [],
        "answered_keys": [],
        "images": [],
    },
    "GenerationResult": {"questions": [], "changes": []},
    "CanonicalListing": {"claims": [], "attribute_sources": {}},
    "ErrorReport": {},
    "RuleSet": {},
}


@pytest.mark.parametrize("name", sorted(_NEW_IN_1_2_0))
def test_1_1_0_payloads_still_parse(name: str) -> None:
    model = next(m for m in MODELS if m.__name__ == name)
    raw = json.loads((GOLDEN_1_1_0 / f"{name}.json").read_text(encoding="utf-8"))
    assert raw.get("schema_version", "1.1.0") == "1.1.0"
    dumped = model.model_validate(raw).model_dump(by_alias=True, mode="json")
    for key, default in _NEW_IN_1_2_0[name].items():
        assert key not in raw
        assert dumped.pop(key) == default
    assert dumped.get("schema_version") == raw.get("schema_version")


def test_1_1_0_errors_and_rules_parse_with_no_gate() -> None:
    report = ErrorReport.model_validate_json(
        (GOLDEN_1_1_0 / "ErrorReport.json").read_text(encoding="utf-8")
    )
    assert report.errors and all(item.gate is None for item in report.errors)
    rules = RuleSet.model_validate_json((GOLDEN_1_1_0 / "RuleSet.json").read_text(encoding="utf-8"))
    assert rules.rules and all(rule.rule_def.gate is None for rule in rules.rules)
    result = GenerationResult.model_validate_json(
        (GOLDEN_1_1_0 / "GenerationResult.json").read_text(encoding="utf-8")
    )
    assert result.listing is not None and result.listing.claims == []
    assert result.questions == [] and result.changes == []
