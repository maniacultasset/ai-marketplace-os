"""Feedback item 20: the committed Amazon and Flipkart error maps cover the documented issues,
load clean, map to the client's taxonomy, and reach the field they name.

- Amazon: the codes on Amazon's Listings Items troubleshooting and Manage Listings Issues pages.
- Flipkart: the API reference documents the error shape but no code list, so the rows match the
  top-level payload node an issue's JSON `path` names (`path:<root>`)."""

from __future__ import annotations

from typing import Any, Final

import pytest
from app.adapters.error_map import ErrorMapper, ErrorMapRow
from app.adapters.flipkart import constants as fk
from app.adapters.flipkart.adapter import issue_keys, path_root
from app.adapters.flipkart.client import error_rows
from app.cli import ERROR_MAP_AMAZON_PATH, ERROR_MAP_FLIPKART_PATH
from app.db.platform_loads import ErrorMapIn, load_error_map_file
from app.services.resolution.history import GROUPS, group_of
from mkos_schemas.codes import ERROR_CODE_META, ErrorCode
from mkos_schemas.enums import Marketplace

# the client's taxonomy (feedback item 20); `unknown` is only for an unmapped issue
TAXONOMY: Final = frozenset(
    {
        "content_data",
        "missing_attribute",
        "image",
        "identifier_exemption",
        "category_mismatch",
        "account_permission",
        "marketplace_system",
    }
)
# the adapter's safe repair kinds (`adapters/amazon/repairs.py`)
REPAIR_RULES: Final = frozenset(
    {
        "enum_fold",
        "unit_format",
        "identifier_normalize",
        "exemption_flag",
        "fill_from_facts",
        "drop_locked",
        "image_slot",
        "fill_variant_axis",
    }
)
AMAZON_DOCS: Final = (
    "https://developer-docs.amazon.com/sp-api/docs/listings-items-api-issues-troubleshooting",
    "https://developer-docs.amazon.com/sp-api/docs/manage-listings-issues",
)
FLIPKART_DOC: Final = "https://seller.flipkart.com/api-docs/listing-api-docs/LMAPIRef.html"
# Flipkart settings Kuinn never sends in v1 (`constants.FORBIDDEN_ATTRIBUTES`): the seller sets
# them in Flipkart Seller Hub
OUTSIDE_ROOTS: Final = frozenset(
    {
        "tax",
        "listing_status",
        "shipping_fees",
        "fulfillment_profile",
        "fulfillment",
        "packages",
        "locations",
        "dating_label",
    }
)


@pytest.fixture(scope="module")
def amazon() -> list[ErrorMapIn]:
    return load_error_map_file(ERROR_MAP_AMAZON_PATH)


@pytest.fixture(scope="module")
def flipkart() -> list[ErrorMapIn]:
    return load_error_map_file(ERROR_MAP_FLIPKART_PATH)


def _row(r: ErrorMapIn) -> ErrorMapRow:
    names = (
        "match_on", "raw_value", "version", "error_code", "error_class", "retry_class",
        "severity", "auto_fixable", "fix_action", "field_hint", "plain_text", "seller_action",
        "enabled",
    )  # fmt: skip
    return ErrorMapRow(marketplace=r.marketplace.value, **{n: getattr(r, n) for n in names})


# ---- both files ----


@pytest.mark.parametrize("path", [ERROR_MAP_AMAZON_PATH, ERROR_MAP_FLIPKART_PATH])
def test_file_loads_with_unique_keys(path: Any) -> None:
    rows = load_error_map_file(path)
    assert rows
    keys = [(r.marketplace, r.match_on, r.raw_value, r.version) for r in rows]
    assert len(keys) == len(set(keys)), path.name
    # one version per key here: a later version is a reviewed edit, not a duplicate
    assert len({k[:3] for k in keys}) == len(keys), path.name
    assert len({r.marketplace for r in rows}) == 1, path.name


@pytest.mark.parametrize("path", [ERROR_MAP_AMAZON_PATH, ERROR_MAP_FLIPKART_PATH])
def test_every_class_is_in_the_client_taxonomy(path: Any) -> None:
    for row in load_error_map_file(path):
        assert group_of(row.error_class.value) in TAXONOMY, row.raw_value
        assert row.error_code in ERROR_CODE_META, row.raw_value


def test_every_class_has_a_group() -> None:
    from mkos_schemas.enums import ErrorClass

    assert set(GROUPS) == {c.value for c in ErrorClass} - {"unknown"}
    assert group_of("unknown") == "unknown" and group_of(None) == "unknown"


@pytest.mark.parametrize("path", [ERROR_MAP_AMAZON_PATH, ERROR_MAP_FLIPKART_PATH])
def test_no_row_turns_on_an_automatic_fix(path: Any) -> None:
    """A map row reports; only the adapter's repairer fixes (ADR-0028). No row sets a fix action
    or a retry class that resends on its own."""
    for row in load_error_map_file(path):
        assert row.auto_fixable is False and row.fix_action is None, row.raw_value
        assert row.retry_class.value == "user_action", row.raw_value
        assert row.severity.value == "error", row.raw_value
        assert row.seller_action, row.raw_value
        assert "{" not in row.plain_text, row.raw_value


# ---- Amazon ----

COVERED: Final = (
    # missing values and counts
    "90220", "4000002", "99022",
    # invalid values, types, enums, patterns, fabric
    "4000001", "90180", "90244", "8333", "90194", "18280", "100095",
    # text rules and lengths
    "90199", "90196", "90225", "90226",
    # counts, numbers, two values that must agree
    "90248", "99016", "90114", "90221", "90197", "90193", "90147", "4005022",
    # identifiers and the catalogue
    "90188", "4005011", "4005013", "4005015", "4005008", "4005006", "4005038", "100980",
    "101165", "101166", "4005039",
    # brand and category eligibility
    "4005007", "4005018", "4005019", "5995", "5886", "5887", "4000003", "101158",
    # price and currency, and Amazon's own side
    "19037", "19038", "18701", "4005001", "4000000",
)  # fmt: skip


def test_amazon_covers_the_documented_codes(amazon: list[ErrorMapIn]) -> None:
    codes = {r.raw_value for r in amazon if r.match_on == "code"}
    assert set(COVERED) <= codes, sorted(set(COVERED) - codes)
    assert len(amazon) >= 90


def test_new_amazon_rows_cite_amazon_pages(amazon: list[ErrorMapIn]) -> None:
    for row in amazon[24:]:  # the rows after the SI9 and SI13 set
        ref = row.policy_ref or {}
        assert ref.get("source") == "sp-api-docs", row.raw_value
        assert ref.get("doc") in AMAZON_DOCS, row.raw_value
        assert ref.get("observed") is False, row.raw_value
        assert "Amazon" in row.plain_text, row.raw_value


def test_kuinn_repair_names_only_safe_repair_kinds(amazon: list[ErrorMapIn]) -> None:
    named = [(r.raw_value, (r.policy_ref or {}).get("kuinn_repair")) for r in amazon]
    named = [(code, rules) for code, rules in named if rules is not None]
    assert named
    for code, rules in named:
        assert isinstance(rules, list) and rules, code
        assert set(rules) <= REPAIR_RULES, code


@pytest.mark.parametrize(
    ("code", "field", "group", "want"),
    [
        ("90244", "attributes.color", "content_data", ErrorCode.VALUE_NOT_ALLOWED),
        ("4000002", "attributes.fabric_type", "missing_attribute", ErrorCode.MISSING_ATTRIBUTE),
        ("90188", None, "identifier_exemption", ErrorCode.IDENTIFIER_INVALID),
        ("4005018", None, "category_mismatch", ErrorCode.CATEGORY_MISMATCH),
        ("5886", None, "account_permission", ErrorCode.ACCOUNT_PERMISSION_DENIED),
        ("4000000", None, "marketplace_system", ErrorCode.MARKETPLACE_UNAVAILABLE),
        ("90225", "content.title", "content_data", ErrorCode.FIELD_TOO_LONG),
        ("19038", None, "content_data", ErrorCode.VALUE_NOT_ALLOWED),
    ],
)
def test_amazon_codes_map_to_cause_field_and_group(
    amazon: list[ErrorMapIn], code: str, field: str | None, group: str, want: ErrorCode
) -> None:
    mapper = ErrorMapper([_row(r) for r in amazon])
    item = mapper.to_item(raw_code=code, raw_message="Raw text.", field=field)
    assert item.error_code is want
    assert item.raw_code == code
    assert item.message.startswith("Amazon ") and item.message.endswith(
        "Marketplace message: Raw text."
    )
    assert item.suggestion
    assert group_of(item.error_class.value if item.error_class else None) == group
    row = next(r for r in amazon if r.raw_value == code)
    assert item.field == (field or row.field_hint or "listing")


def test_identifier_and_price_rows_point_at_the_field(amazon: list[ErrorMapIn]) -> None:
    by_code = {r.raw_value: r for r in amazon if r.match_on == "code"}
    assert by_code["90188"].field_hint == "facts.identifier"
    for code in ("19037", "19038", "18701"):
        assert by_code[code].field_hint == "facts.selling_price"
    for code in ("4005018", "4005019"):
        assert by_code[code].field_hint == "facts.category"


# ---- Flipkart ----


def test_flipkart_rows_match_documented_path_roots(flipkart: list[ErrorMapIn]) -> None:
    assert {r.match_on for r in flipkart} == {"category"}
    roots = {r.raw_value.removeprefix(fk.PATH_CATEGORY_PREFIX) for r in flipkart}
    assert roots == set(fk.PATH_ROOTS)
    for row in flipkart:
        assert row.marketplace is Marketplace.FLIPKART
        assert row.raw_value.startswith(fk.PATH_CATEGORY_PREFIX)
        ref = row.policy_ref or {}
        assert ref.get("doc") == FLIPKART_DOC and ref.get("observed") is False
        assert "Flipkart" in row.plain_text


def test_flipkart_outside_rows_are_the_settings_kuinn_never_sends(
    flipkart: list[ErrorMapIn],
) -> None:
    outside = {
        r.raw_value.removeprefix(fk.PATH_CATEGORY_PREFIX)
        for r in flipkart
        if (r.policy_ref or {}).get("handled_outside") is True
    }
    assert outside == OUTSIDE_ROOTS
    for row in flipkart:
        if row.raw_value.removeprefix(fk.PATH_CATEGORY_PREFIX) in OUTSIDE_ROOTS:
            assert "Seller Hub" in (row.seller_action or ""), row.raw_value


@pytest.mark.parametrize(
    ("path", "root"),
    [
        ("price.mrp", "price"),
        ("$.price.sellingPrice", "price"),
        ("SKU-1.tax.hsn", "tax"),
        ("locations[0].inventory", "locations"),
        ("/shipping_fees/local", "shipping_fees"),
        ("attributes.colour", None),
        ("", None),
    ],
)
def test_path_root(path: str, root: str | None) -> None:
    assert path_root({"path": path}) == root


def test_path_root_ignores_a_drifted_path() -> None:
    assert path_root({"path": 7}) is None
    assert path_root({}) is None


def test_issue_keys_price_path_names_the_price_block() -> None:
    names, categories = issue_keys({"path": "price.mrp", "code": 400})
    assert names == [fk.PRICE_ATTRIBUTE]
    assert categories == ["path:price"]
    names, categories = issue_keys({"attribute": "title", "category": "X", "path": "tax.hsn"})
    assert names == ["title"] and categories == ["X", "path:tax"]


def test_error_rows_reads_errors_then_attribute_errors() -> None:
    a, b = {"code": 1}, {"code": 2, "path": "price.mrp"}
    assert error_rows({"errors": [a], "attribute_errors": [b]}) == [a, b]
    assert error_rows({"errors": "drifted", "attribute_errors": [b]}) == [b]
    assert error_rows({}) == []


@pytest.mark.parametrize(
    ("root", "want", "hint"),
    [
        ("price", ErrorCode.VALUE_NOT_ALLOWED, "facts.selling_price"),
        ("tax", ErrorCode.MISSING_ATTRIBUTE, None),
        ("product_id", ErrorCode.IDENTIFIER_INVALID, None),
        ("address_label", ErrorCode.MISSING_ATTRIBUTE, "facts.manufacturer"),
    ],
)
def test_flipkart_path_maps_through_the_mapper(
    flipkart: list[ErrorMapIn], root: str, want: ErrorCode, hint: str | None
) -> None:
    mapper = ErrorMapper([_row(r) for r in flipkart])
    _names, categories = issue_keys({"path": f"{root}.x"})
    item = mapper.to_item(
        raw_code="12345",
        raw_message="Raw.",
        field=None,
        marketplace=Marketplace.FLIPKART,
        categories=categories,
    )
    assert item.error_code is want
    assert item.field == (hint or "listing")
    assert item.message.startswith("Flipkart ")
    assert item.source.value == "flipkart_adapter"


def test_flipkart_unknown_path_stays_unknown(flipkart: list[ErrorMapIn]) -> None:
    mapper = ErrorMapper([_row(r) for r in flipkart])
    item = mapper.to_item(
        raw_code="12345",
        raw_message="Odd.",
        field=None,
        marketplace=Marketplace.FLIPKART,
        categories=[],
    )
    assert item.error_code is ErrorCode.MARKETPLACE_UNKNOWN_ERROR
