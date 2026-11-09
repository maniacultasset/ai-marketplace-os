"""SI14 T15 (AC7): the deterministic column mapper (exact, alias, fuzzy; one key per column)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from app.services.imports.column_map import (
    EXTENDED_ALIASES,
    FUZZY_MIN,
    TARGETS,
    identity_of,
    map_headers,
)
from app.services.imports.headers import check_header
from hypothesis import given, settings
from hypothesis import strategies as st
from mkos_schemas.codes import ErrorCode

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "column_headers_si14.json"
SHEETS = json.loads(FIXTURE.read_text(encoding="utf-8"))["sheets"]
BAD = {ErrorCode.HEADER_DUPLICATE_COLUMN, ErrorCode.HEADER_UNKNOWN_COLUMN}


def _keys(headers: list[str]) -> list[str | None]:
    return [m.key for m in map_headers(headers)]


@pytest.mark.parametrize("sheet", SHEETS, ids=[s["name"] for s in SHEETS])
def test_fixture_sheets(sheet: dict[str, object]) -> None:
    expected = sheet["headers"]
    assert isinstance(expected, dict)
    assert _keys(list(expected)) == list(expected.values())


@pytest.mark.parametrize("sheet", SHEETS, ids=[s["name"] for s in SHEETS])
def test_mapped_header_list_passes_check_header(sheet: dict[str, Any]) -> None:
    headers = list(sheet["headers"])
    mapped = [m.key for m in map_headers(headers) if m.key is not None]
    assert {code for code, _ in check_header(mapped)} & BAD == set()


def test_rungs_and_confidences() -> None:
    exact, alias, fuzzy, miss = map_headers(["seller_sku", "Colour", "Patern", "zzzz"])
    assert (exact.method, exact.confidence, exact.needs_review) == ("exact", 1.0, False)
    assert (alias.method, alias.key, alias.confidence) == ("alias", "color_name", 1.0)
    assert fuzzy.method == "fuzzy" and fuzzy.key == "pattern"
    assert FUZZY_MIN <= fuzzy.confidence < 1.0 and not fuzzy.needs_review
    assert (miss.key, miss.method, miss.needs_review) == (None, "none", True)


def test_fuzzy_threshold() -> None:
    above = map_headers(["fabrik_type"])[0]  # ratio about 0.91
    assert above.key == "fabric_type" and above.confidence >= 0.85
    below = map_headers(["fbrc tp"])[0]
    assert below.key is None and below.needs_review


def test_two_fuzzy_claims_the_higher_wins() -> None:
    first, second = map_headers(["Patterns", "Patern"])
    assert {first.key, second.key} == {"pattern", None}
    winner = first if first.key else second
    loser = second if first.key else first
    assert loser.method == "none" and winner.confidence > 0


def test_an_exact_key_is_never_stolen_by_a_fuzzy_match() -> None:
    fuzzy, exact = map_headers(["patern", "pattern"])
    assert exact.key == "pattern" and exact.method == "exact"
    assert fuzzy.key is None


@pytest.mark.parametrize("header", ["", "   ", "﻿", "﻿  "])
def test_blank_headers_need_review(header: str) -> None:
    only = map_headers([header])[0]
    assert only.key is None and only.needs_review


def test_an_alias_clashing_with_an_exact_column_is_none() -> None:
    assert _keys(["seller_sku", "Item SKU"]) == ["seller_sku", None]


def test_bom_is_dropped() -> None:
    assert map_headers(["﻿seller_sku"])[0].key == "seller_sku"


def test_unicode_is_none() -> None:
    assert map_headers(["रंग"])[0].key is None


def test_two_hundred_headers_are_fast() -> None:
    headers = [f"Column header number {n}" for n in range(200)]
    start = time.perf_counter()
    map_headers(headers)
    assert time.perf_counter() - start < 0.2  # ~13 ms locally; headroom for a loaded CI runner


def test_every_alias_value_is_a_target_and_every_target_is_accepted() -> None:
    assert set(EXTENDED_ALIASES.values()) <= set(TARGETS)
    for target in TARGETS:
        codes = {code for code, _ in check_header([target])}
        assert ErrorCode.HEADER_UNKNOWN_COLUMN not in codes, target


def test_targets_hold_no_bullet_or_export_only_name() -> None:
    assert not [t for t in TARGETS if t.startswith("bullet_")]
    assert "title" not in TARGETS and "keywords" not in TARGETS


def test_bullets_map_to_themselves() -> None:
    mapped = map_headers(["bullet_1", "bullet_2", "bullet_3"])
    assert [m.key for m in mapped] == ["bullet_1", "bullet_2", "bullet_3"]
    assert not any(m.needs_review for m in mapped)


def test_grams_aliases_clash_with_package_weight() -> None:
    assert _keys(["package_weight_g"]) == ["package_weight_g"]
    assert _keys(["package_weight", "package_weight_g"]) == ["package_weight", None]


def test_export_only_name_keeps_itself() -> None:
    title = map_headers(["title"])[0]
    assert (title.key, title.method) == ("title", "exact")


_header = st.one_of(
    st.sampled_from([*TARGETS, *EXTENDED_ALIASES, "bullet_1", "bullet_2", "Patern", "Colur"]),
    st.text(max_size=20),
)


@given(st.lists(_header, max_size=40))
@settings(max_examples=150, deadline=None)
def test_property_no_shared_identity(headers: list[str]) -> None:
    mapped = map_headers(headers)
    assert len(mapped) == len(headers)
    identities = [identity_of(m.key) for m in mapped if m.key is not None]
    assert len(identities) == len(set(identities))
