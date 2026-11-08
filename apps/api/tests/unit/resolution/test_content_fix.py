"""SI11 T15: the deterministic content fix of a marketplace refusal (pure) and the Amazon
adapter's limit rule."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from app.adapters.amazon.repairs import content_limit
from app.services.resolution.content import content_fixes
from mkos_schemas.listing import CanonicalListing

TITLE = "Cotton Kurta Set for Women with Straight Fit and Round Neck."


@dataclass
class Row:
    field_path: str = "content.title"
    error_code: str = "FIELD_TOO_LONG"
    severity: str = "error"
    source: str = "amazon_adapter"
    message: str = "too long"
    raw_message: str | None = None
    marketplace_code: str | None = "99301"
    id: uuid.UUID = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.id = self.id or uuid.uuid4()


def _listing(**content: Any) -> CanonicalListing:
    return CanonicalListing.model_validate(
        {
            "listing_id": str(uuid.uuid4()),
            "product_id": str(uuid.uuid4()),
            "marketplace": "amazon",
            "category": "ethnic_wear_kurta_sets",
            "locale": "en-IN",
            "content": {
                "title": TITLE,
                "bullets": ["Soft cotton fabric for daily wear"],
                "description": "A cotton kurta set.",
                **content,
            },
            "attributes": {},
        }
    )


def test_the_title_is_trimmed_to_the_limit_and_nothing_else_changes() -> None:
    base = _listing()
    row = Row()
    fixed, fixes = content_fixes(base, [row], lambda r: 58)
    assert [f.issue_id for f in fixes] == [row.id]
    assert len(fixed.content.title) <= 58 and TITLE.startswith(fixed.content.title)
    assert fixed.model_dump(exclude={"content": {"title"}}) == base.model_dump(
        exclude={"content": {"title"}}
    )
    assert fixes[0].record.field == "content.title"


@pytest.mark.parametrize(
    ("row", "limit"),
    [
        (Row(), None),  # no limit known
        (Row(), 10),  # beyond the SI4 share cap
        (Row(), 500),  # already within
        (Row(error_code="BANNED_TERM"), 58),  # not a length refusal
        (Row(field_path="attributes.color"), 58),  # not content
        (Row(severity="warning"), 58),
    ],
)
def test_no_fix_without_a_safe_trim(row: Row, limit: int | None) -> None:
    base = _listing()
    fixed, fixes = content_fixes(base, [row], lambda r: limit)
    assert fixes == () and fixed == base


def test_one_fix_per_path() -> None:
    a, b = Row(), Row()
    _fixed, fixes = content_fixes(_listing(), [a, b], lambda r: 58)
    assert len(fixes) == 1


SCHEMA = {
    "properties": {
        "item_name": {"items": {"properties": {"value": {"type": "string", "maxLength": 120}}}}
    }
}


def test_the_stored_definition_wins_over_the_message() -> None:
    text = "The value for 'item_name' is longer than 150 characters."
    assert content_limit(SCHEMA, "content.title", text) == 120
    assert content_limit(None, "content.title", text) == 150
    assert content_limit(None, "content.bullets[2]", "maximum length of 255 characters") == 255
    assert content_limit(None, "keywords.search_terms", "exceeds the maximum of 250 chars") == 250


@pytest.mark.parametrize(
    ("path", "text"),
    [
        ("content.title", "The title is wrong."),
        ("attributes.color", "longer than 150 characters"),
        ("content.title", "longer than 0 characters"),
    ],
)
def test_no_limit(path: str, text: str) -> None:
    assert content_limit(None, path, text) is None
