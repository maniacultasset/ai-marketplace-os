"""Golden fixture round-trips."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mkos_schemas.export import MODELS
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
    assert len(MODELS) == 18


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
