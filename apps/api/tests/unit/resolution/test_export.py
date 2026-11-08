"""SI13 T16 (AC16; P:B5, D17): the Auto / Seller / Marketplace export, pure."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from typing import Any

import pytest
from app.services.resolution.export import rows, to_csv, to_md

DOC = {"source": "sp-api-docs", "doc": "https://developer-docs.amazon.com/x", "observed": False}


@dataclass
class Row:
    raw_value: str
    error_code: str
    error_class: str
    plain_text: str = "Amazon says so."
    policy_ref: Any = None
    version: int = 1
    enabled: bool = True
    marketplace: str = "amazon"
    match_on: str = "code"
    field_hint: str | None = None


def test_the_four_labels() -> None:
    table = rows(
        [
            Row("4005033", "VALUE_NOT_ALLOWED", "invalid_value", policy_ref=DOC),
            Row("4005007", "ACCOUNT_PERMISSION_DENIED", "account_permission"),
            Row("4005013", "SKU_CONFLICT", "identifier_error"),
            Row("90220", "MISSING_ATTRIBUTE", "missing_data"),
            Row("500", "RATE_LIMITED", "marketplace_technical"),
        ]
    )
    who = {r.code: r.who for r in table}
    assert who["4005007"] == "Marketplace"
    assert who["4005013"] == "Kuinn team"
    assert set(who.values()) <= {"Auto", "Seller", "Marketplace", "Kuinn team"}
    assert {r.code: r.doc for r in table}["4005033"] == DOC["doc"]
    assert {r.code: r.doc for r in table}["4005007"] == "—"


def test_highest_enabled_version_only() -> None:
    table = rows(
        [
            Row("1", "VALUE_NOT_ALLOWED", "invalid_value", "old", version=1),
            Row("1", "VALUE_NOT_ALLOWED", "invalid_value", "new", version=2),
            Row("2", "VALUE_NOT_ALLOWED", "invalid_value", enabled=False),
        ]
    )
    assert [(r.code, r.plain_text) for r in table] == [("1", "new")]


def test_markdown_escapes_and_counts() -> None:
    out = to_md(rows([Row("1", "VALUE_NOT_ALLOWED", "invalid_value", "a | b\nc")]), 4)
    assert "a \\| b c" in out and out.rstrip().endswith("Codes seen with no row yet: 4")


def test_csv_round_trips_and_empty_is_header_only() -> None:
    out = to_csv(rows([Row("1", "VALUE_NOT_ALLOWED", "invalid_value", 'He said "x", ok')]))
    parsed = list(csv.reader(io.StringIO(out)))
    assert parsed[1][4] == 'He said "x", ok'
    empty = list(csv.reader(io.StringIO(to_csv([]))))
    assert empty[0][0] == "Marketplace" and empty[1][0] == "unmapped"


@pytest.mark.parametrize("start", ["=", "+", "-", "@", "\t", "\r"])
def test_formula_start_is_neutralised(start: str) -> None:
    text = f"{start}1+1"
    out = to_csv(rows([Row("1", "VALUE_NOT_ALLOWED", "invalid_value", text)]))
    _, row, _ = list(csv.reader(io.StringIO(out)))
    assert row[4] == "'" + text


def test_an_equals_in_the_middle_is_unchanged() -> None:
    out = to_csv(rows([Row("1", "VALUE_NOT_ALLOWED", "invalid_value", "a = b")]))
    assert list(csv.reader(io.StringIO(out)))[1][4] == "a = b"


def test_the_lane_follows_the_field_hint_and_the_recategorise_context() -> None:
    """Round 1 fix: the table routes these codes as the live planner does (F:103, F:106)."""
    table = rows(
        [
            Row(
                "100980", "IDENTIFIER_DUPLICATE", "identifier_error", field_hint="facts.identifier"
            ),
            Row(
                "4005015", "IDENTIFIER_DUPLICATE", "identifier_error", field_hint="facts.identifier"
            ),
            Row("4000003", "CATEGORY_MISMATCH", "category_mismatch", field_hint="facts.category"),
            Row("101158", "CATEGORY_MISMATCH", "category_mismatch", field_hint="facts.category"),
            Row("9", "VALUE_NOT_ALLOWED", "invalid_value", field_hint="not a path!"),
        ]
    )
    who = {r.code: r.who for r in table}
    assert who["100980"] == who["4005015"] == "Seller"
    assert who["4000003"] == who["101158"] == "Seller (Kuinn team once live)"
    assert who["9"] in {"Auto", "Seller", "Marketplace", "Kuinn team"}
