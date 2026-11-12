"""SI9 T3 (AC1): the committed Amazon error map, `app/db/seed_data/error_map.amazon.json`.

Every row comes from Amazon's published SP-API material (`policy_ref.observed` false, the page in
`policy_ref.doc`); observed prod codes later arrive as version 2 rows."""

from __future__ import annotations

import json
from typing import Any, Final

import pytest
from app.adapters.error_map import ErrorMapRow
from app.cli import ERROR_MAP_AMAZON_PATH
from app.db.platform_loads import ErrorMapIn, load_error_map_file
from app.services.auto_fix.policy import Route, route
from app.services.issues import classify
from mkos_schemas.codes import ERROR_CODE_META, ErrorCode
from mkos_schemas.enums import ErrorClass
from mkos_schemas.errors import ErrorItem
from tests.adapters.amazon_support import SELLER, amazon_adapter
from tests.support.fake_marketplace import build_fake

from .test_amazon_adapter import _five

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


# Where a doc-sourced row's meaning comes from: Amazon's SP-API docs, Amazon's public seller
# pages (sell.amazon.com), or, for a code whose Amazon help needs a login, the public integrator
# guides that agree on it (the row keeps generic text and a `note` saying so)
DOC_SOURCES: Final = frozenset({"sp-api-docs", "amazon-seller-docs", "public-integrator-docs"})


def assert_documented(row: ErrorMapIn) -> None:
    """SI13 (CP-D20): a seed row comes from Amazon's published SP-API material (`sp-api-docs`, an
    https `doc`, `user_action`) or from a staff-approved AI proposal (a UUID `proposal_id` and
    `approved_by`, no email, the code's own retry class). Any other source fails."""
    import uuid as _uuid

    assert row.marketplace.value == "amazon", row.raw_value
    assert row.error_code in ERROR_CODE_META, row.raw_value
    assert row.seller_action, row.raw_value
    assert row.fix_action is None and row.auto_fixable is False, row.raw_value
    assert row.severity.value == "error", row.raw_value
    assert not row.raw_value.startswith("FAKE"), row.raw_value
    ref = row.policy_ref or {}
    assert ref.get("observed") is False, row.raw_value
    if ref.get("source") == "staff-approved-ai-proposal":
        _uuid.UUID(str(ref.get("proposal_id")))
        _uuid.UUID(str(ref.get("approved_by")))
        assert "@" not in json.dumps(ref) and "doc" not in ref, row.raw_value
        assert row.retry_class == ERROR_CODE_META[row.error_code].retry_class, row.raw_value
        return
    assert ref.get("source") in DOC_SOURCES, row.raw_value
    assert row.retry_class.value == "user_action", row.raw_value
    assert str(ref.get("doc", "")).startswith("https://"), row.raw_value
    for extra in ref.get("also", []):
        assert str(extra).startswith("https://"), row.raw_value
    if ref.get("source") == "public-integrator-docs":
        # Amazon's own help for the code needs a Seller Central login: say so on the row
        assert "login" in str(ref.get("note", "")), row.raw_value


def test_every_row_is_a_documented_amazon_row(rows: list[ErrorMapIn]) -> None:
    for row in rows:
        assert_documented(row)


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


# ---- SI9 T4 (AC3): the rows through the fake SP-API reach SI4's routes ----


_ROW_FIELDS: Final = (
    "marketplace",
    "match_on",
    "raw_value",
    "version",
    "error_code",
    "error_class",
    "retry_class",
    "severity",
    "auto_fixable",
    "fix_action",
    "field_hint",
    "plain_text",
    "seller_action",
    "enabled",
)
MISSING_TEXT: Final = "Amazon needs a value that this listing does not send."


def _rows() -> list[ErrorMapRow]:
    out = []
    for r in load_error_map_file(ERROR_MAP_AMAZON_PATH):
        values = {name: getattr(r, name) for name in _ROW_FIELDS}
        values["marketplace"] = r.marketplace.value
        out.append(ErrorMapRow(**values))
    return out


def _issue(
    code: str, attribute: str | None, message: str = "Bad.", categories: list[str] | None = None
) -> dict[str, Any]:
    out: dict[str, Any] = {"code": code, "message": message, "severity": "ERROR"}
    if attribute is not None:
        out["attributeNames"] = [attribute]
    if categories is not None:
        out["categories"] = categories
    return out


async def _write(issue: dict[str, Any]) -> ErrorItem:
    app = build_fake()
    sent = _five()
    app.state.fake.spapi_issues[sent.seller_sku] = [issue]
    result = await amazon_adapter(app, rows=_rows()).create_listing(sent)
    path = "content.title" if issue.get("attributeNames") == ["item_name"] else "listing"
    error = next(f.error for f in result.fields if f.field == path)
    assert isinstance(error, ErrorItem)
    return error


async def _read_errors(issue: dict[str, Any]) -> list[ErrorItem]:
    app = build_fake()
    adapter = amazon_adapter(app, rows=_rows())
    sent = _five()
    await adapter.create_listing(sent)
    app.state.fake.spapi_items[(SELLER, sent.seller_sku)]["issues"] = [issue]
    got = await adapter.get_listing(sent.seller_sku, listing_id=sent.listing_id)
    return list(got.errors)


RAW_90220: Final = "'item_name' is required but not supplied."


async def test_write_90220_maps_to_missing_attribute() -> None:
    error = await _write(_issue("90220", "item_name", RAW_90220))
    assert error.field == "content.title"
    assert error.error_code is ErrorCode.MISSING_ATTRIBUTE
    assert error.message == f"{MISSING_TEXT} Marketplace message: {RAW_90220}"
    assert error.suggestion == "Add the missing value to the product, then send the listing again."
    assert error.raw_code == "90220"
    assert error.error_class is ErrorClass.MISSING_DATA


async def test_read_90220_maps_the_same() -> None:
    written = await _write(_issue("90220", "item_name", RAW_90220))
    (read,) = await _read_errors(_issue("90220", "item_name", RAW_90220))
    assert (read.field, read.error_code, read.message, read.suggestion, read.raw_code) == (
        written.field,
        written.error_code,
        written.message,
        written.suggestion,
        written.raw_code,
    )


async def test_unmapped_code_with_known_category() -> None:
    error = await _write(_issue("77777", "item_name", categories=["INVALID_ATTRIBUTE"]))
    assert error.error_code is ErrorCode.VALUE_NOT_ALLOWED
    assert error.error_class is ErrorClass.INVALID_VALUE
    assert error.raw_code == "77777"


async def test_unmapped_code_with_unknown_category() -> None:
    error = await _write(_issue("77777", "item_name", "Odd.", categories=["SOMETHING_ELSE"]))
    assert error.error_code is ErrorCode.MARKETPLACE_UNKNOWN_ERROR
    assert error.error_class is ErrorClass.UNKNOWN
    assert error.message == "Odd."


@pytest.mark.parametrize("code", ["", "   ", "9" * 200])
async def test_blank_or_long_code_is_unknown(code: str) -> None:
    error = await _write(_issue(code, "item_name", "Odd."))
    assert error.error_code is ErrorCode.MARKETPLACE_UNKNOWN_ERROR
    assert error.raw_code is None or len(error.raw_code) <= 128


async def test_unicode_message_kept() -> None:
    error = await _write(_issue("90220", "item_name", "शीर्षक आवश्यक है।"))
    assert error.message.endswith("Marketplace message: शीर्षक आवश्यक है।")


async def test_item_level_issue_reads_as_plain_language() -> None:
    error = await _write(_issue("5665", None, "Brand X.", categories=["QUALIFICATION_REQUIRED"]))
    assert error.field == "listing"
    # 5665 has its own code row now, which wins over the QUALIFICATION_REQUIRED category row
    assert error.error_code is ErrorCode.ACCOUNT_PERMISSION_DENIED
    assert error.message.startswith("Amazon has not approved your account to use this brand")
    assert "attributes." not in error.message and "amazon " not in error.message


def _mapped(code: str, field: str, categories: list[str]) -> ErrorItem:
    """An item as the adapter maps it, through the committed rows."""
    from app.adapters.error_map import ErrorMapper

    return ErrorMapper(_rows()).to_item(
        raw_code=code, raw_message="Raw.", field=field, categories=categories
    )


@pytest.mark.parametrize(
    ("code", "field", "categories", "want", "want_route", "want_kind"),
    [
        ("90220", "attributes.brand", [], ErrorCode.MISSING_ATTRIBUTE, Route.ASK, "missing_fact"),
        (
            "77777",
            "attributes.color",
            ["INVALID_ATTRIBUTE"],
            ErrorCode.VALUE_NOT_ALLOWED,
            Route.DETERMINISTIC,
            None,
        ),
        (
            "77777",
            "listing",
            ["QUALIFICATION_REQUIRED"],
            ErrorCode.API_REJECTED_PARTIAL,
            Route.ASK,
            "marketplace_error_review",
        ),
        ("18027", "images[0]", [], ErrorCode.IMAGE_NON_COMPLIANT, Route.ASK, "image_replacement"),
    ],
)
def test_mapped_codes_reach_si4_routes(
    code: str,
    field: str,
    categories: list[str],
    want: ErrorCode,
    want_route: Route,
    want_kind: str | None,
) -> None:
    item = _mapped(code, field, categories)
    assert item.error_code is want
    got = route(item, source="amazon_adapter", critical_keys=frozenset())
    assert got.route is want_route
    if want_kind is not None:
        assert got.task_kind == want_kind
    else:
        assert got.fix_action is not None and got.fix_action.value == "remap_value"
    # the stored row's auto_fixable comes from the item and the code metadata (SI4 classify);
    # the rows set no fix_action (D5), so no Amazon row is shown as auto-fixable
    assert classify(item, critical_keys=frozenset(), source="amazon_adapter")[2] is None


# ---- SI13 T10 (AC7): the v2 code rows from Amazon's public troubleshooting page ----

SI13_CODES: Final = {
    "4000003": ("CATEGORY_MISMATCH", "facts.category"),
    "101158": ("CATEGORY_MISMATCH", "facts.category"),
    "100980": ("IDENTIFIER_DUPLICATE", "facts.identifier"),
    "4005015": ("IDENTIFIER_DUPLICATE", "facts.identifier"),
    "4005007": ("ACCOUNT_PERMISSION_DENIED", None),
    "4005008": ("ACCOUNT_PERMISSION_DENIED", None),
    "4005006": ("IDENTIFIER_INVALID", "facts.identifier"),
    "4005038": ("IDENTIFIER_INVALID", "facts.identifier"),
    "4005013": ("SKU_CONFLICT", None),
    "101168": ("SKU_CONFLICT", None),
    "4005033": ("VALUE_NOT_ALLOWED", None),
}
LEFT_OUT: Final = ("8541",)  # kept out on purpose (KNOWN-ISSUES, V1 checklist); 8572: item 13


def test_si13_rows_present_and_documented(rows: list[ErrorMapIn]) -> None:
    by_code = {r.raw_value: r for r in rows if r.match_on == "code"}
    for code, (error_code, hint) in SI13_CODES.items():
        row = by_code[code]
        assert row.error_code.value == error_code, code
        assert ErrorCode(error_code) in ERROR_CODE_META
        assert row.field_hint == hint, code
        doc = str((row.policy_ref or {}).get("doc", ""))
        assert doc.startswith("https://developer-docs.amazon.com/sp-api/"), code
        assert "sellercentral" not in doc, code
    for code in LEFT_OUT:
        assert code not in by_code, code


def test_si13_rows_route_to_their_lanes(rows: list[ErrorMapIn]) -> None:
    import uuid

    from app.services.resolution.plan import IssueView, PlanContext, plan

    by_code = {r.raw_value: r for r in rows if r.match_on == "code"}

    def lane(code: str, **ctx: object) -> tuple[str, str]:
        row = by_code[code]
        view = IssueView(
            uuid.uuid4(),
            "amazon_adapter",
            row.field_hint or "listing",
            row.error_code.value,
            row.error_class.value,
            "error",
            marketplace_code=code,
        )
        (step,) = plan([view], PlanContext(**ctx))  # type: ignore[arg-type]
        return step.lane, step.action

    assert lane("4000003", recategorizable=True) == ("seller", "recategorize")
    assert lane("101158") == ("staff", "recategorize_review")
    assert lane("100980") == ("seller", "edit_identifier")
    assert lane("4005007")[0] == "marketplace"
    assert lane("4005013") == ("staff", "sku_conflict")
    assert lane("101168") == ("staff", "sku_conflict")
    assert ErrorClass.ACCOUNT_PERMISSION.value == by_code["4005008"].error_class.value


# ---- Prod codes with no row before: 5665, 5461, 8542, 8560, 99010, 8105 ----

PROD_CODES: Final = {
    # code: (error code, error class, field hint, lane with no listing context)
    "5665": ("ACCOUNT_PERMISSION_DENIED", "account_permission", None, "marketplace"),
    "5461": ("ACCOUNT_PERMISSION_DENIED", "account_permission", None, "marketplace"),
    "8542": ("IDENTIFIER_DUPLICATE", "identifier_error", "facts.identifier", "seller"),
    "8560": ("MISSING_ATTRIBUTE", "missing_data", None, "staff"),
    "99010": ("MISSING_ATTRIBUTE", "missing_data", None, "staff"),
    "8105": ("VALUE_NOT_ALLOWED", "invalid_value", None, "staff"),
}


def test_prod_code_rows_present(rows: list[ErrorMapIn]) -> None:
    # the version 1 rows (8560 has a version 2 since item 13)
    by_code = {r.raw_value: r for r in rows if r.match_on == "code" and r.version == 1}
    for code, (error_code, klass, hint, _lane) in PROD_CODES.items():
        row = by_code[code]
        assert (row.error_code.value, row.error_class.value, row.field_hint) == (
            error_code,
            klass,
            hint,
        ), code
        assert row.version == 1 and row.enabled, code
        source = (row.policy_ref or {}).get("source")
        # only Amazon's public seller page documents the brand codes; the rest are generic
        want = "amazon-seller-docs" if code in ("5665", "5461") else "public-integrator-docs"
        assert source == want, code


@pytest.mark.parametrize("code", sorted(PROD_CODES))
def test_prod_code_maps_through_the_mapper(code: str) -> None:
    error_code, klass, _hint, _lane = PROD_CODES[code]
    item = _mapped(code, "listing", [])
    assert item.error_code is ErrorCode(error_code)
    assert item.error_class is ErrorClass(klass)
    assert item.raw_code == code
    assert item.message.startswith("Amazon "), code
    assert item.message.endswith("Marketplace message: Raw."), code
    assert item.suggestion, code


@pytest.mark.parametrize("code", ["5665", "8542", "8105"])
async def test_prod_code_maps_on_write(code: str) -> None:
    app = build_fake()
    sent = _five()
    app.state.fake.spapi_issues[sent.seller_sku] = [_issue(code, None, "Raw.")]
    result = await amazon_adapter(app, rows=_rows()).create_listing(sent)
    (error,) = [f.error for f in result.fields if f.error is not None]
    assert error.field == (PROD_CODES[code][2] or "listing")
    assert error.error_code is ErrorCode(PROD_CODES[code][0])
    assert error.raw_code == code
    assert "Marketplace message: Raw." in error.message


async def test_prod_code_maps_on_read() -> None:
    (read,) = await _read_errors(_issue("99010", "item_name", "Raw."))
    assert read.error_code is ErrorCode.MISSING_ATTRIBUTE
    assert read.raw_code == "99010"


def test_prod_code_lanes(rows: list[ErrorMapIn]) -> None:
    from app.services.resolution.plan import lane_for

    by_code = {r.raw_value: r for r in rows if r.match_on == "code"}
    for code, (_error_code, _klass, _hint, want) in PROD_CODES.items():
        row = by_code[code]
        got = lane_for(
            row.error_code.value, row.error_class.value, field_path=row.field_hint or "listing"
        )
        assert got == want, code


def test_8542_is_a_catalog_conflict_not_a_permission() -> None:
    """8542 is a barcode matched to catalog products whose details differ (no approval fixes it):
    on a named attribute it asks the seller, not the marketplace lane."""
    import uuid

    from app.services.resolution.plan import QUALIFICATION_CODES, IssueView, PlanContext, plan

    assert "8542" not in QUALIFICATION_CODES
    view = IssueView(
        uuid.uuid4(),
        "amazon_adapter",
        "attributes.color",
        "IDENTIFIER_DUPLICATE",
        "identifier_error",
        "error",
        marketplace_code="8542",
    )
    (step,) = plan([view], PlanContext())
    assert step.lane != "marketplace"


# ---- Item 13: GTIN exemption and barcode refusals lead back to the barcode choice ----

GTIN_CODES: Final = {
    # code: (error code, policy_ref source)
    "8550": ("IDENTIFIER_MISSING", "public-integrator-docs"),
    "8572": ("IDENTIFIER_DUPLICATE", "public-integrator-docs"),
    "90188": ("IDENTIFIER_INVALID", "sp-api-docs"),
}


def _best(rows: list[ErrorMapIn]) -> dict[str, ErrorMapIn]:
    """Each code's highest enabled version: the row the mapper uses."""
    out: dict[str, ErrorMapIn] = {}
    for r in rows:
        if r.match_on == "code" and r.enabled:
            held = out.get(r.raw_value)
            if held is None or r.version > held.version:
                out[r.raw_value] = r
    return out


def test_gtin_rows_present_and_documented(rows: list[ErrorMapIn]) -> None:
    best = _best(rows)
    for code, (error_code, source) in GTIN_CODES.items():
        row = best[code]
        assert (row.error_code.value, row.error_class.value) == (error_code, "identifier_error")
        assert row.field_hint == "facts.identifier", code
        assert row.version == 1, code
        assert (row.policy_ref or {}).get("source") == source, code
        # the seller is sent back to the product's barcode
        assert "barcode" in (row.seller_action or "").lower(), code
    assert "exemption" in (best["8550"].seller_action or "").lower()
    doc = str((best["90188"].policy_ref or {}).get("doc"))
    assert doc.startswith("https://developer-docs.amazon.com/sp-api/"), doc


def test_8560_version_2_names_the_unapproved_exemption(rows: list[ErrorMapIn]) -> None:
    """Integrators report Amazon's 8560 when a GTIN exemption is claimed for a brand or category
    Amazon has not approved: version 2 says so and keeps the code's class."""
    versions = sorted(r.version for r in rows if r.match_on == "code" and r.raw_value == "8560")
    assert versions == [1, 2]
    row = _best(rows)["8560"]
    assert row.version == 2
    assert (row.error_code.value, row.error_class.value, row.field_hint) == (
        "MISSING_ATTRIBUTE",
        "missing_data",
        None,
    )
    text = f"{row.plain_text} {row.seller_action}".lower()
    assert "gtin exemption" in text and "brand" in text and "category" in text
    assert len((row.policy_ref or {}).get("also", [])) >= 1


@pytest.mark.parametrize("code", sorted(GTIN_CODES))
def test_gtin_code_maps_through_the_mapper_to_the_barcode(code: str) -> None:
    item = _mapped(code, "", [])
    assert item.field == "facts.identifier"
    assert item.error_code is ErrorCode(GTIN_CODES[code][0])
    assert item.message.startswith("Amazon "), code
    assert item.message.endswith("Marketplace message: Raw."), code
    assert item.suggestion and "barcode" in item.suggestion.lower(), code


def test_8560_maps_to_version_2() -> None:
    item = _mapped("8560", "listing", [])
    assert item.error_code is ErrorCode.MISSING_ATTRIBUTE
    assert "GTIN exemption" in (item.suggestion or "")


def test_gtin_rows_route_to_the_seller_barcode_edit(rows: list[ErrorMapIn]) -> None:
    import uuid

    from app.services.resolution.plan import IssueView, PlanContext, plan

    best = _best(rows)
    for code in GTIN_CODES:
        row = best[code]
        view = IssueView(
            uuid.uuid4(),
            "amazon_adapter",
            row.field_hint or "listing",
            row.error_code.value,
            row.error_class.value,
            "error",
            marketplace_code=code,
        )
        (step,) = plan([view], PlanContext())
        assert (step.lane, step.action) == ("seller", "edit_identifier"), code
