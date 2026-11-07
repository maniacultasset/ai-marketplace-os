"""SI9 T3 (AC1): the committed Amazon error map, `app/db/seed_data/error_map.amazon.json`.

Every row comes from Amazon's published SP-API material (`policy_ref.observed` false, the page in
`policy_ref.doc`); observed prod codes later arrive as version 2 rows."""

from __future__ import annotations

from typing import Final

import pytest
from app.cli import ERROR_MAP_AMAZON_PATH
from app.db.platform_loads import ErrorMapIn, load_error_map_file
from mkos_schemas.codes import ERROR_CODE_META

# SP-API Listings Items 2021-08-01 `Issue.categories` (the model's enum table)
CATEGORIES: Final = frozenset(
    {
        "MISSING_ATTRIBUTE",
        "INVALID_ATTRIBUTE",
        "INVALID_IMAGE",
        "MISSING_IMAGE",
        "QUALIFICATION_REQUIRED",
    }
)


@pytest.fixture(scope="module")
def rows() -> list[ErrorMapIn]:
    return load_error_map_file(ERROR_MAP_AMAZON_PATH)


def test_file_parses(rows: list[ErrorMapIn]) -> None:
    assert rows
    assert ERROR_MAP_AMAZON_PATH.name == "error_map.amazon.json"


def test_every_row_is_a_documented_amazon_row(rows: list[ErrorMapIn]) -> None:
    for row in rows:
        assert row.marketplace.value == "amazon", row.raw_value
        assert row.error_code in ERROR_CODE_META, row.raw_value
        assert row.seller_action, row.raw_value
        assert row.fix_action is None and row.auto_fixable is False, row.raw_value
        assert row.retry_class.value == "user_action", row.raw_value
        assert row.severity.value == "error", row.raw_value
        ref = row.policy_ref or {}
        assert ref.get("source") == "sp-api-docs", row.raw_value
        assert ref.get("observed") is False, row.raw_value
        assert str(ref.get("doc", "")).startswith("https://"), row.raw_value
        assert not row.raw_value.startswith("FAKE"), row.raw_value


def test_plain_text_is_plain_language(rows: list[ErrorMapIn]) -> None:
    """Class-level text that names Amazon and no canonical path: `{field}` would render a path
    such as `attributes.brand`, and `{marketplace}` the lowercase enum."""
    for row in rows:
        assert "{" not in row.plain_text, row.raw_value
        assert "Amazon" in row.plain_text or "Amazon" in (row.seller_action or ""), row.raw_value


def test_keys_unique(rows: list[ErrorMapIn]) -> None:
    keys = [(r.match_on, r.raw_value, r.version) for r in rows]
    assert len(keys) == len(set(keys))


def test_category_rows_are_sp_api_categories(rows: list[ErrorMapIn]) -> None:
    categories = {r.raw_value for r in rows if r.match_on == "category"}
    assert categories == CATEGORIES
