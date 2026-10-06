"""`ProductCreate.category` is optional in the published contract while the service refuses a
create without it (`products.category` is NOT NULL, DATABASE.md:410). Until the team decides (make it
required in the contract, or make the column nullable), that mismatch must stay an open
TEAM-QUESTIONS row, so it cannot ship unrecorded. Once the field is required, this test is moot."""

from __future__ import annotations

from pathlib import Path

from app.api.v1.schemas.products import ProductCreate

_QUESTIONS = Path(__file__).resolve().parents[4] / "docs/review/TEAM-QUESTIONS.md"


def open_rows_naming(text: str, needle: str) -> list[str]:
    return [
        line
        for line in text.splitlines()
        if line.startswith("| T") and needle in line and "| open |" in line
    ]


def test_optional_category_in_the_contract_has_an_open_team_question() -> None:
    if ProductCreate.model_fields["category"].is_required():
        return
    rows = open_rows_naming(_QUESTIONS.read_text(encoding="utf-8"), "`ProductCreate.category`")
    assert rows, "the contract/runtime category mismatch needs an open TEAM-QUESTIONS row"


def test_open_rows_naming_ignores_answered_rows() -> None:
    text = "| T1 | `ProductCreate.category` | x | y | z | answered | user |\n"
    assert open_rows_naming(text, "`ProductCreate.category`") == []
    assert open_rows_naming(text.replace("answered", "open"), "`ProductCreate.category`")
