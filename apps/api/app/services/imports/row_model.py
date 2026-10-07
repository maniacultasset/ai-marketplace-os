"""The CSV row model (KNOWN-ISSUES.md:47; S13 PD10, Q4, Q7; DATABASE.md:402-424, :1104-1112).

`normalize_row(raw)` runs once per row, before any rule: every cell is trimmed, inner whitespace
runs collapse to one space, a blank cell becomes None (absent, never an error by itself), and an
alias header becomes its A.6 name. `validate_row(norm)` then applies every rule in one pass and
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
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Final
from urllib.parse import urlsplit

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource, Severity
from mkos_schemas.errors import ErrorItem

from app.services.imports.headers import (
    GRAMS_COLUMN,
    IGNORED_WITH_WARNING,
    check_header,
    normalize_header,
    row_key,
)

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
HSN_PATTERN: Final = re.compile(r"[0-9]{4}|[0-9]{6}|[0-9]{8}")
MONEY_PATTERN: Final = re.compile(r"[0-9]{1,10}(\.[0-9]{1,2})?")
TAX_PATTERN: Final = re.compile(r"[0-9]{1,3}(\.[0-9]{1,2})?")
DIMENSION_PATTERN: Final = re.compile(r"[0-9]{1,5}(\.[0-9]{1,2})?")
WEIGHT_PATTERN: Final = re.compile(r"([0-9]{1,7}(?:\.[0-9]{1,3})?)\s?(g|kg)", re.IGNORECASE)
GRAMS_PATTERN: Final = re.compile(r"[0-9]{1,7}")
GTIN_PATTERN: Final = re.compile(r"[0-9]{8}|[0-9]{12,14}")
COUNTRY_PATTERN: Final = re.compile(r"[A-Za-z]{2}")
SIZE_KEY_PATTERN: Final = re.compile(r"[a-z0-9][a-z0-9 ./+-]{0,31}")
EMAIL_PATTERN: Final = re.compile(r"[^@\s]+@[^@\s]+")
PHONE_PATTERN: Final = re.compile(r"[+]?[0-9][0-9 -]{5,19}")
_WHITESPACE: Final = re.compile(r"\s+")
_URL_SPLIT: Final = re.compile(r"[|,;\s]+")

GENDERS: Final = frozenset({"men", "women", "unisex", "boys", "girls"})
IDENTIFIER_TYPES: Final = frozenset({"gtin", "ean", "upc"})
MAX_TITLE: Final = 500
MAX_BRAND: Final = 100
MAX_PARTY_TEXT: Final = 300
MAX_COLOUR: Final = 64
MAX_EMAIL_BYTES: Final = 254
MAX_MEDIA_URLS: Final = 9
MAX_URL: Final = 2_048
MAX_GRAMS: Final = 2_000_000
MAX_TAX: Final = Decimal(100)

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


def gtin_valid(value: str) -> bool:
    """The GS1 check digit over GTIN-8/12/13/14, left-padded to 14 (the SQL `gtin_valid` twin)."""
    if not GTIN_PATTERN.fullmatch(value):
        return False
    digits = [int(d) for d in value.zfill(14)]
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(digits[:13]))
    return (10 - total % 10) % 10 == digits[13]


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

    def media(self) -> tuple[str, ...]:
        urls: list[str] = []
        for key in ("main_image", "additional_images"):
            cell = self.get(key)
            if cell is None:
                continue
            for candidate in (u for u in _URL_SPLIT.split(cell) if u):
                if self._url_ok(key, candidate):
                    urls.append(candidate)
        size_chart = self.get("size_chart")
        if size_chart is not None:
            self._url_ok("size_chart", size_chart)
        if len(urls) > MAX_MEDIA_URLS:
            self.fail(ErrorCode.VALUE_NOT_ALLOWED, f"at most {MAX_MEDIA_URLS} media URLs per row")
            return ()
        if not urls:
            self.fail(
                ErrorCode.REQUIRED_FIELD_MISSING,
                "main_image (or additional_images) needs at least one https URL",
            )
        return tuple(urls)

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


def validate_row(norm: Mapping[str, str | None]) -> RowResult:
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
    sub_category = c.slug("sub_category", c.get("sub_category"))
    product_type = c.slug("product_type", c.get("product_type"))
    gender = c.get("gender")
    if gender is not None:
        gender = gender.lower()
        if gender not in GENDERS:
            c.fail(ErrorCode.VALUE_NOT_ALLOWED, "gender must be men, women, unisex, boys or girls")
            gender = None

    manufacturer = c.text("manufacturer_name", MAX_PARTY_TEXT)
    address = c.text("manufacturer_address", MAX_PARTY_TEXT)
    if address is not None and manufacturer is None and c.get("manufacturer_name") is None:
        c.fail(ErrorCode.REQUIRED_FIELD_MISSING, "manufacturer_address needs manufacturer_name")

    id_type, id_value = _identifier(c)

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
        if not COUNTRY_PATTERN.fullmatch(country):
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
    urls = c.media()
    attributes = {key: value for key in ATTRIBUTE_COLUMNS if (value := c.get(key)) is not None}
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
    )
    return RowResult(row, [], c.warnings)


def _identifier(c: _Checker) -> tuple[str | None, str | None]:
    id_type = c.get("product_identifier_type")
    id_value = c.get("product_identifier")
    if id_type is not None:
        id_type = id_type.lower()
        if id_type not in IDENTIFIER_TYPES:
            c.fail(ErrorCode.VALUE_NOT_ALLOWED, "product_identifier_type must be gtin, ean or upc")
            return None, None
    if (id_type is None) != (id_value is None):
        c.fail(
            ErrorCode.CROSS_FIELD_MISMATCH,
            "product_identifier_type and product_identifier go together",
        )
        return None, None
    if id_value is not None and not gtin_valid(id_value):
        c.fail(
            ErrorCode.IDENTIFIER_INVALID,
            "product_identifier must be 8, 12, 13 or 14 digits with a valid check digit",
        )
        return None, None
    return id_type, id_value


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
