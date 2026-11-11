"""The CSV row model (KNOWN-ISSUES.md:47; S13 PD10, Q4, Q7; DATABASE.md:402-424, :1104-1112).

`normalize_row(raw)` runs once per row, before any rule: every cell is trimmed, inner whitespace
runs collapse to one space, a blank cell becomes None (absent, never an error by itself), and an
alias header becomes its A.6 name. `validate_row(norm, brands=...)` then applies every rule in one pass and
returns a `RowResult`: the typed `ProductImportRow` when no rule failed, every error, and the
warnings (content, marketplace and workflow cells are export-only in v1 and never stored).

The bounds are the database's own (DATABASE.md:1108-1112, written out here so a row this model
accepts never trips a CHECK): money `numeric(12,2)` > 0; tax 0..100, `numeric(5,2)`; HSN 4, 6 or
8 digits; dimensions `numeric(7,2)` > 0, all three or none; weights whole grams 1..2,000,000;
GTIN-8/12/13/14 with a valid GS1 check digit (leading zeros kept, the cell is text). A media cell
holds `https` URLs only (never fetched; at most 9, each ≤ 2,048 characters) and every row needs at
least one (site L7929). There is no `status` column: an imported product is always `draft`.

Every error is an `ErrorItem` at `file.row` with source `schema` whose message names the A.6
column from this module's own text, never the cell (no `value_submitted`, Q7).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Final
from urllib.parse import urlsplit

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource, Severity
from mkos_schemas.errors import ErrorItem

from app.core.identifiers import IDENTIFIER_TYPES, check, gtin_valid, type_from_length
from app.core.resolved_facts import match_brand
from app.services.composition import CompositionError, normalized, parse_composition
from app.services.imports.headers import (
    GRAMS_COLUMN,
    IDENTIFIER_ALIASES,
    IGNORED_WITH_WARNING,
    check_header,
    normalize_header,
    row_key,
)
from app.services.intake_values import COUNTRY_PATTERN, DIMENSION_PATTERN, HSN_PATTERN, MAX_GRAMS
from app.services.iso_countries import ISO_3166_ALPHA2
from app.services.package_sense import package_warnings

__all__ = [
    "ATTRIBUTE_COLUMNS",
    "ProductImportRow",
    "RowResult",
    "check_header",
    "gtin_valid",
    "normalize_row",
    "row_error",
    "validate_row",
]

ROW_FIELD: Final = "file.row"
# The API `Sku` pattern read as ECMA-262 (JSON Schema): its `\s` also covers U+FEFF, which Python's
# `\s` does not, so it is named (an imported SKU is later returned by the products API).
SKU_PATTERN: Final = re.compile(r"[^\]\s\ufeff]{1,64}")
SLUG_PATTERN: Final = re.compile(r"[a-z0-9_]{2,64}")
MONEY_PATTERN: Final = re.compile(r"[0-9]{1,10}(\.[0-9]{1,2})?")
TAX_PATTERN: Final = re.compile(r"[0-9]{1,3}(\.[0-9]{1,2})?")
WEIGHT_PATTERN: Final = re.compile(r"([0-9]{1,7}(?:\.[0-9]{1,3})?)\s?(g|kg)", re.IGNORECASE)
GRAMS_PATTERN: Final = re.compile(r"[0-9]{1,7}")
SIZE_KEY_PATTERN: Final = re.compile(r"[a-z0-9][a-z0-9 ./+-]{0,31}")
EMAIL_PATTERN: Final = re.compile(r"[^@\s]+@[^@\s]+")
PHONE_PATTERN: Final = re.compile(r"[+]?[0-9][0-9 -]{5,19}")
_WHITESPACE: Final = re.compile(r"\s+")
_URL_SPLIT: Final = re.compile(r"[|,;\s]+")

GENDERS: Final = frozenset({"men", "women", "unisex", "boys", "girls", "baby"})
MAX_TITLE: Final = 500
MAX_BRAND: Final = 100
MAX_PARTY_TEXT: Final = 300
MAX_COLOUR: Final = 64
MAX_EMAIL_BYTES: Final = 254
MAX_MEDIA_URLS: Final = 9
MAX_URL: Final = 2_048
MAX_TAX: Final = Decimal(100)
MAX_STYLE_NUMBER: Final = 64

ATTRIBUTE_COLUMNS: Final[tuple[str, ...]] = (
    "fit",
    "pattern",
    "fabric_type",
    "fiber_composition",
    "lining",
    "stretch",
    "neckline",
    "collar_type",
    "sleeve_type",
    "sleeve_length",
    "garment_length",
    "rise",
    "leg_style",
    "closure",
    "pocket_type",
    "occasion",
    "season",
    "wash_care",
    "iron_care",
)
"""A.6 attribute and care columns stored as `attributes.<same key>` facts (plain text; a cell
that looks like JSON is still text)."""


@dataclass(frozen=True, slots=True)
class ProductImportRow:
    sku: str
    title: str
    category: str
    parent_sku: str | None = None
    brand_name: str | None = None
    sub_category: str | None = None
    product_type: str | None = None
    gender: str | None = None
    manufacturer_name: str | None = None
    manufacturer_address: str | None = None
    identifier_type: str | None = None
    identifier_value: str | None = None
    # SI5: `identifier_exemption` (a style row's value reaches its children that give neither)
    identifier_exemption: bool | None = None
    color_name: str | None = None
    normalized_color: str | None = None
    size_name: str | None = None
    size_key: str | None = None
    size_system: str | None = None
    country_of_origin: str | None = None
    mrp: Decimal | None = None
    selling_price: Decimal | None = None
    tax_rate: Decimal | None = None
    hsn_code: str | None = None
    consumer_care_email: str | None = None
    consumer_care_phone: str | None = None
    package_weight_g: int | None = None
    package_length_cm: Decimal | None = None
    package_breadth_cm: Decimal | None = None
    package_height_cm: Decimal | None = None
    image_urls: tuple[str, ...] = ()
    attributes: Mapping[str, str] = field(default_factory=dict)
    # SI1 D9
    style_number: str | None = None
    size_chart_url: str | None = None
    fiber_fibres: tuple[dict[str, object], ...] = ()

    @property
    def colour_key(self) -> str | None:
        return None if self.color_name is None else self.color_name.strip().lower()


@dataclass(frozen=True, slots=True)
class RowResult:
    row: ProductImportRow | None
    errors: list[ErrorItem] = field(default_factory=list)
    warnings: list[ErrorItem] = field(default_factory=list)


def row_error(
    code: ErrorCode,
    message: str,
    source: ErrorSource = ErrorSource.SCHEMA,
    severity: Severity = Severity.ERROR,
) -> ErrorItem:
    return ErrorItem(
        field=ROW_FIELD, error_code=code, message=message, source=source, retryable=False,
        severity=severity,
    )  # fmt: skip


def normalize_row(raw: Mapping[str, str | None]) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for header, cell in raw.items():
        key = row_key(normalize_header(header))
        if key is None or (key in out and out[key] is not None):
            continue
        text = _WHITESPACE.sub(" ", cell).strip() if isinstance(cell, str) else ""
        out[key] = text or None
    return out


class _Checker:
    def __init__(self, norm: Mapping[str, str | None]) -> None:
        self.norm = norm
        self.errors: list[ErrorItem] = []
        self.warnings: list[ErrorItem] = []

    def fail(self, code: ErrorCode, message: str) -> None:
        self.errors.append(row_error(code, message))

    def get(self, key: str) -> str | None:
        return self.norm.get(key)

    def required(self, key: str) -> str | None:
        value = self.get(key)
        if value is None:
            self.fail(ErrorCode.REQUIRED_FIELD_MISSING, f"{key} is required")
        return value

    def text(self, key: str, limit: int) -> str | None:
        value = self.get(key)
        if value is not None and len(value) > limit:
            self.fail(ErrorCode.FIELD_TOO_LONG, f"{key} is longer than {limit} characters")
            return None
        return value

    def slug(self, key: str, value: str | None) -> str | None:
        if value is None:
            return None
        slug = value.lower()
        if not SLUG_PATTERN.fullmatch(slug):
            self.fail(ErrorCode.PATTERN_MISMATCH, f"{key} must be 2-64 of a-z, 0-9 and _")
            return None
        return slug

    def decimal(
        self, key: str, value: str | None, pattern: re.Pattern[str], shape: str
    ) -> Decimal | None:
        if value is None:
            return None
        if not pattern.fullmatch(value):
            self.fail(ErrorCode.PATTERN_MISMATCH, f"{key} must be {shape}")
            return None
        try:
            number = Decimal(value)
        except InvalidOperation:  # pragma: no cover - the pattern admits only digits and a dot
            self.fail(ErrorCode.PATTERN_MISMATCH, f"{key} must be {shape}")
            return None
        return number

    def positive(self, key: str, number: Decimal | None) -> Decimal | None:
        if number is not None and number <= 0:
            self.fail(ErrorCode.VALUE_NOT_ALLOWED, f"{key} must be greater than 0")
            return None
        return number

    def money(self, key: str) -> Decimal | None:
        raw = self.get(key)
        if raw is None:
            return None
        cleaned = raw.replace("₹", "").replace(",", "").replace(" ", "")
        if cleaned.upper().startswith("INR"):
            cleaned = cleaned[3:]
        shape = "a positive amount with at most 10 digits before and 2 after the point"
        return self.positive(key, self.decimal(key, cleaned or None, MONEY_PATTERN, shape))

    def grams(self) -> int | None:
        with_unit = self.get("package_weight")
        bare = self.get(GRAMS_COLUMN)
        if with_unit is not None:
            match = WEIGHT_PATTERN.fullmatch(with_unit)
            if match is None:
                self.fail(
                    ErrorCode.PATTERN_MISMATCH,
                    "package_weight needs a unit: a number then g or kg (450 g, 0.45 kg)",
                )
                return None
            amount = Decimal(match.group(1)) * (1000 if match.group(2).lower() == "kg" else 1)
            if amount != amount.to_integral_value():
                self.fail(ErrorCode.PATTERN_MISMATCH, "package_weight must be whole grams")
                return None
            return self._grams_bound("package_weight", int(amount))
        if bare is not None:
            if not GRAMS_PATTERN.fullmatch(bare):
                self.fail(ErrorCode.PATTERN_MISMATCH, "package_weight_g must be whole grams")
                return None
            return self._grams_bound("package_weight_g", int(bare))
        return None

    def _grams_bound(self, key: str, grams: int) -> int | None:
        if not 1 <= grams <= MAX_GRAMS:
            self.fail(ErrorCode.VALUE_NOT_ALLOWED, f"{key} must be 1 to {MAX_GRAMS} grams")
            return None
        return grams

    def media(self, *, warn_without: bool) -> tuple[tuple[str, ...], str | None]:
        """The image URLs and the size chart URL. SI1 D9(d): a row without an image URL imports;
        a standalone or style row gets a warning (images come through the product form)."""
        urls: list[str] = []
        for key in ("main_image", "additional_images"):
            cell = self.get(key)
            if cell is None:
                continue
            for candidate in (u for u in _URL_SPLIT.split(cell) if u):
                if self._url_ok(key, candidate):
                    urls.append(candidate)
        size_chart = self.get("size_chart")
        chart_url = (
            size_chart
            if size_chart is not None and self._url_ok("size_chart", size_chart)
            else None
        )
        if len(urls) > MAX_MEDIA_URLS:
            self.fail(ErrorCode.VALUE_NOT_ALLOWED, f"at most {MAX_MEDIA_URLS} media URLs per row")
            return (), chart_url
        if (
            not urls
            and warn_without
            and not any(self.get(key) is not None for key in ("main_image", "additional_images"))
        ):
            self.warnings.append(
                row_error(
                    ErrorCode.REQUIRED_FIELD_MISSING,
                    "no image URL; upload images in the product form",
                    severity=Severity.WARNING,
                )
            )
        return tuple(urls), chart_url

    def _url_ok(self, key: str, url: str) -> bool:
        if len(url) > MAX_URL:
            self.fail(ErrorCode.FIELD_TOO_LONG, f"a {key} URL is longer than {MAX_URL} characters")
            return False
        try:
            parts = urlsplit(url)
        except ValueError:
            parts = None
        ok = (
            parts is not None
            and parts.scheme == "https"
            and bool(parts.hostname)
            and parts.username is None
            and parts.password is None
        )
        if not ok:
            self.fail(ErrorCode.PATTERN_MISMATCH, f"{key} must hold https URLs")
        return ok


def validate_row(norm: Mapping[str, str | None], *, brands: Sequence[str]) -> RowResult:
    """Every rule over one normalized row. `brands` (SI8 CP-D9) are the tenant's own brand names
    (`BrandPromptRepository.brand_choices()`): a `brand_name` cell must be one of them (casefold
    after strip) and is stored in that name's spelling."""
    c = _Checker(norm)
    sku = c.required("seller_sku")
    if sku is not None and not SKU_PATTERN.fullmatch(sku):
        if len(sku) > 64:
            c.fail(ErrorCode.FIELD_TOO_LONG, "seller_sku is longer than 64 characters")
        else:
            c.fail(ErrorCode.PATTERN_MISMATCH, "seller_sku may not contain spaces or ]")
        sku = None
    parent = c.get("parent_style_id")
    if parent is not None and not SKU_PATTERN.fullmatch(parent):
        c.fail(ErrorCode.PATTERN_MISMATCH, "parent_style_id may not contain spaces or ]")
        parent = None
    if parent is not None and parent == sku:
        c.fail(ErrorCode.CROSS_FIELD_MISMATCH, "parent_style_id names the row's own seller_sku")
    title = c.required("product_name")
    title = c.text("product_name", MAX_TITLE) if title is not None else None
    category = c.slug("category", c.required("category"))
    brand = c.text("brand_name", MAX_BRAND)
    if brand is not None:
        matched = match_brand(brand, brands)
        if matched is None:
            c.fail(ErrorCode.VALUE_NOT_ALLOWED, "brand_name must be one of your brand names")
        brand = matched
    sub_category = c.slug("sub_category", c.get("sub_category"))
    product_type = c.slug("product_type", c.get("product_type"))
    gender = c.get("gender")
    if gender is not None:
        gender = gender.lower()
        if gender not in GENDERS:
            c.fail(
                ErrorCode.VALUE_NOT_ALLOWED,
                "gender must be men, women, unisex, boys, girls or baby",
            )
            gender = None

    manufacturer = c.text("manufacturer_name", MAX_PARTY_TEXT)
    address = c.text("manufacturer_address", MAX_PARTY_TEXT)
    if address is not None and manufacturer is None and c.get("manufacturer_name") is None:
        c.fail(ErrorCode.REQUIRED_FIELD_MISSING, "manufacturer_address needs manufacturer_name")

    id_type, id_value, exemption = _identifier(c)

    colour = c.text("color_name", MAX_COLOUR)
    size_name = c.get("size_name")
    size_key = c.get("normalized_size") or size_name
    if size_key is not None:
        size_key = size_key.lower()
        if not SIZE_KEY_PATTERN.fullmatch(size_key):
            c.fail(ErrorCode.PATTERN_MISMATCH, "normalized_size (or size_name) is not a size key")
            size_key = None

    country = c.get("country_of_origin")
    if country is not None:
        if not COUNTRY_PATTERN.fullmatch(country) or country.upper() not in ISO_3166_ALPHA2:
            c.fail(ErrorCode.PATTERN_MISMATCH, "country_of_origin must be an ISO alpha-2 code")
            country = None
        else:
            country = country.upper()

    mrp = c.money("mrp")
    price = c.money("selling_price")
    if mrp is not None and price is not None and price > mrp:
        c.fail(ErrorCode.PRICE_ABOVE_MRP, "selling_price is above mrp")
    tax_raw = c.get("tax_rate")
    tax = c.decimal(
        "tax_rate",
        tax_raw.removesuffix("%").strip() if tax_raw is not None else None,
        TAX_PATTERN,
        "a percentage from 0 to 100 with at most 2 decimals",
    )
    if tax is not None and tax > MAX_TAX:
        c.fail(ErrorCode.VALUE_NOT_ALLOWED, "tax_rate must be from 0 to 100")
        tax = None
    hsn = c.get("hsn_code")
    if hsn is not None and not HSN_PATTERN.fullmatch(hsn):
        c.fail(ErrorCode.PATTERN_MISMATCH, "hsn_code must be 4, 6 or 8 digits")
        hsn = None

    email = c.get("consumer_care_email")
    if email is not None and (
        not EMAIL_PATTERN.fullmatch(email) or len(email.encode()) > MAX_EMAIL_BYTES
    ):
        c.fail(ErrorCode.PATTERN_MISMATCH, "consumer_care_email is not an email address")
        email = None
    phone = c.get("consumer_care_phone")
    if phone is not None and not PHONE_PATTERN.fullmatch(phone):
        c.fail(ErrorCode.PATTERN_MISMATCH, "consumer_care_phone is not a phone number")
        phone = None

    grams = c.grams()
    dims = _dimensions(c)
    _package_sense(c, category, dims, grams)
    # a child row's images come from its style (S22): no warning on a row with a parent
    urls, chart_url = c.media(warn_without=parent is None)
    attributes = {key: value for key in ATTRIBUTE_COLUMNS if (value := c.get(key)) is not None}
    fibres: tuple[dict[str, object], ...] = ()
    composition = attributes.get("fiber_composition")
    if composition is not None:
        try:
            fibres = tuple(normalized(parse_composition(composition)))
        except CompositionError:
            c.fail(
                ErrorCode.VALUE_NOT_ALLOWED,
                "fiber_composition must list fibres whose percents add up to 100",
            )
    style_number = c.text("style_number", MAX_STYLE_NUMBER)
    if style_number is not None and "\x00" in style_number:
        c.fail(ErrorCode.VALUE_NOT_ALLOWED, "style_number must not contain NUL")
        style_number = None
    for key in sorted(IGNORED_WITH_WARNING):
        if c.get(key) is not None:
            c.warnings.append(
                row_error(
                    ErrorCode.VALUE_NOT_ALLOWED,
                    f"{key} is not imported in v1; the cell was kept but not stored",
                    severity=Severity.WARNING,
                )
            )

    if c.errors or sku is None or title is None or category is None:
        return RowResult(None, c.errors, c.warnings)
    row = ProductImportRow(
        sku=sku,
        title=title,
        category=category,
        parent_sku=parent,
        brand_name=brand,
        sub_category=sub_category,
        product_type=product_type,
        gender=gender,
        manufacturer_name=manufacturer,
        manufacturer_address=address,
        identifier_type=id_type,
        identifier_value=id_value,
        identifier_exemption=exemption,
        color_name=colour,
        normalized_color=c.get("normalized_color"),
        size_name=size_name,
        size_key=size_key,
        size_system=c.get("size_system"),
        country_of_origin=country,
        mrp=mrp,
        selling_price=price,
        tax_rate=tax,
        hsn_code=hsn,
        consumer_care_email=email,
        consumer_care_phone=phone,
        package_weight_g=grams,
        package_length_cm=dims[0],
        package_breadth_cm=dims[1],
        package_height_cm=dims[2],
        image_urls=urls,
        attributes=attributes,
        style_number=style_number,
        size_chart_url=chart_url,
        fiber_fibres=fibres,
    )
    return RowResult(row, [], c.warnings)


_YES: Final = frozenset({"yes", "true", "y", "1"})
_NO: Final = frozenset({"no", "false", "n", "0"})


def _exemption(c: _Checker) -> bool | None:
    raw = c.get("identifier_exemption")
    if raw is None:
        return None
    word = raw.strip().lower()
    if word in _YES:
        return True
    if word in _NO:
        return False
    c.fail(ErrorCode.VALUE_NOT_ALLOWED, "identifier_exemption must be yes or no")
    return None


def _identifier(c: _Checker) -> tuple[str | None, str | None, bool | None]:
    """The barcode `(type, value)` from the A.6 pair or one alias column (`ean`, `upc`, `gtin`,
    `barcode`), checked for its check digit and the type's length, and the exemption."""
    exemption = _exemption(c)
    id_type = c.get("product_identifier_type")
    id_value = c.get("product_identifier")
    aliases = [(name, c.get(name)) for name in IDENTIFIER_ALIASES if c.get(name) is not None]
    if len(aliases) > 1 or (aliases and (id_type is not None or id_value is not None)):
        c.fail(ErrorCode.CROSS_FIELD_MISMATCH, "send one barcode column")
        return None, None, exemption
    if aliases:
        name, id_value = aliases[0]
        assert id_value is not None  # noqa: S101 - filtered above
        id_type = IDENTIFIER_ALIASES[name] or type_from_length(id_value)
        if id_type is None:
            c.fail(ErrorCode.IDENTIFIER_INVALID, "barcode must be 8, 12, 13 or 14 digits")
            return None, None, exemption
    elif id_type is not None:
        id_type = id_type.lower()
        if id_type not in IDENTIFIER_TYPES:
            c.fail(ErrorCode.VALUE_NOT_ALLOWED, "product_identifier_type must be gtin, ean or upc")
            return None, None, exemption
    if (id_type is None) != (id_value is None):
        c.fail(
            ErrorCode.CROSS_FIELD_MISMATCH,
            "product_identifier_type and product_identifier go together",
        )
        return None, None, exemption
    if id_value is not None:
        if not gtin_valid(id_value):
            c.fail(
                ErrorCode.IDENTIFIER_INVALID,
                "product_identifier must be 8, 12, 13 or 14 digits with a valid check digit",
            )
            return None, None, exemption
        if check(id_type, id_value) is not None:
            c.fail(ErrorCode.IDENTIFIER_INVALID, f"{id_type} has the wrong number of digits")
            return None, None, exemption
        if exemption is True:
            c.fail(ErrorCode.CROSS_FIELD_MISMATCH, "a barcode and identifier_exemption conflict")
            return None, None, None
    return id_type, id_value, exemption


_SENSE_COLUMNS: Final = {"package_dimensions": "package size", "package_weight": "package weight"}


def _package_sense(
    c: _Checker,
    category: str | None,
    dims: tuple[Decimal | None, Decimal | None, Decimal | None],
    grams: int | None,
) -> None:
    """Item 15: a box that looks wrong for its category is a warning; the row still imports."""
    sides = None if any(d is None for d in dims) else [d for d in dims if d is not None]
    for key, message in package_warnings(category, sides, grams).items():
        c.warnings.append(
            row_error(
                ErrorCode.VALUE_NOT_ALLOWED,
                f"{_SENSE_COLUMNS[key]}: {message}",
                severity=Severity.WARNING,
            )
        )


def _dimensions(c: _Checker) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    keys = ("package_length_cm", "package_breadth_cm", "package_height_cm")
    given = [c.get(k) for k in keys]
    if any(v is not None for v in given) and not all(v is not None for v in given):
        c.fail(
            ErrorCode.CROSS_FIELD_MISMATCH,
            "package_length_cm, package_breadth_cm and package_height_cm go together",
        )
        return None, None, None
    shape = "a positive number with at most 5 digits before and 2 after the point"
    values = [
        c.positive(k, c.decimal(k, v, DIMENSION_PATTERN, shape))
        for k, v in zip(keys, given, strict=True)
    ]
    if any(v is None for v in values):
        return None, None, None
    return values[0], values[1], values[2]
