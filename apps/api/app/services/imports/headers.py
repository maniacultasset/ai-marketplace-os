"""The CSV header contract (CANON-ADDENDUM A§20; DATABASE.md:970, A.6 :2183-2198; S13 Q4, Q7).

Headers are the 64 BRD master names of A.6 verbatim (`A6_COLUMNS`), plus the aliases DATABASE.md
:970 keeps (`ALIASES`). `normalize_header` runs once per header: a leading BOM is dropped, the
name is trimmed and lower-cased (KNOWN-ISSUES.md:47: a header that matches only after
lower-casing is accepted). `check_header` returns every problem as `(ErrorCode, message)`:

- a missing required column (`REQUIRED`: SKU, name and category) → `HEADER_MISSING_COLUMN`;
- a header that is neither an A.6 name nor an alias → `HEADER_UNKNOWN_COLUMN`;
- two headers that name the same column, after normalisation or through an alias →
  `HEADER_DUPLICATE_COLUMN`.

Messages name our own column names only. An unknown header is echoed only when it matches
`^[a-z0-9_ ]{1,64}$`; anything else is named by its position (`column <n>`), so a crafted header
never reaches a report. `header_report` folds the problems into the one file-level
`ErrorReport` (`IMPORT_FAILED`, field `file.header`, source `schema`).

The content, marketplace and workflow columns of A.6 are export-only or derived in v1
(`IGNORED_WITH_WARNING`): accepted in the header, never stored, and each non-blank cell gives the
row a warning (the row model's).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Final

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource
from mkos_schemas.errors import MAX_ERROR_ITEMS, ErrorItem, ErrorReport

HEADER_FIELD: Final = "file.header"
_BOM: Final = "﻿"
_ECHO_SAFE: Final = re.compile(r"[a-z0-9_ ]{1,64}")
_BULLET: Final = re.compile(r"bullet_[1-9][0-9]?")

A6_COLUMNS: Final[tuple[str, ...]] = (
    # Identity
    "seller_sku",
    "parent_style_id",
    "product_name",
    "brand_name",
    "manufacturer_name",
    "manufacturer_address",
    "product_identifier_type",
    "product_identifier",
    # Classification
    "gender",
    "category",
    "sub_category",
    "product_type",
    # Attributes
    "color_name",
    "normalized_color",
    "size_name",
    "normalized_size",
    "size_system",
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
    # Care
    "wash_care",
    "iron_care",
    # Compliance
    "country_of_origin",
    "mrp",
    "consumer_care_email",
    "consumer_care_phone",
    "hsn_code",
    # Commercial
    "selling_price",
    "tax_rate",
    "package_weight",
    "package_length_cm",
    "package_breadth_cm",
    "package_height_cm",
    # Media
    "main_image",
    "additional_images",
    "size_chart",
    # Content (export-only in v1)
    "title",
    "bullet_1_to_n",
    "description",
    "keywords",
    # Marketplace (adapter or ledger, invariant 4)
    "marketplace_category",
    "marketplace_product_type",
    "marketplace_sku",
    "marketplace_listing_id",
    "marketplace_status",
    "live_url",
    # Workflow (derived, not stored)
    "overall_status",
    "open_issue_count",
    "human_action_required",
    "last_updated",
)

GRAMS_COLUMN: Final = "package_weight_g"
"""The internal name of the bare-grams weight (the `package_weight_g`/`weight_grams` aliases)."""

ALIASES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "parent_sku": "parent_style_id",
        "mrp_inr": "mrp",
        "price_inr": "selling_price",
        "weight_grams": GRAMS_COLUMN,
        "package_weight_g": GRAMS_COLUMN,
    }
)
"""Alias header → the row model's key. Both grams aliases name A.6 `package_weight` in bare
grams; `package_weight` itself needs a `g`/`kg` suffix (DATABASE.md:975)."""

REQUIRED: Final[tuple[str, ...]] = ("seller_sku", "product_name", "category")

IGNORED_WITH_WARNING: Final[frozenset[str]] = frozenset(
    {
        "title",
        "bullet_1_to_n",
        "description",
        "keywords",
        "marketplace_category",
        "marketplace_product_type",
        "marketplace_sku",
        "marketplace_listing_id",
        "marketplace_status",
        "live_url",
        "overall_status",
        "open_issue_count",
        "human_action_required",
        "last_updated",
    }
)

_KNOWN: Final = frozenset(A6_COLUMNS)


def normalize_header(raw: str) -> str:
    return raw.removeprefix(_BOM).strip().lower()


def is_bullet(name: str) -> bool:
    """`bullet_1` … `bullet_99`: the BRD's `bullet_1_to_n`, spelled one column per bullet."""
    return _BULLET.fullmatch(name) is not None


def row_key(name: str) -> str | None:
    """The row model's key for a normalised header: the A.6 name, the alias target, or None for
    an unknown header. Bullet columns are content (`bullet_1_to_n`)."""
    if name in _KNOWN:
        return name
    if name in ALIASES:
        return ALIASES[name]
    if is_bullet(name):
        return "bullet_1_to_n"
    return None


def _column_identity(key: str) -> str:
    """Two headers clash when they name the same A.6 column (the grams aliases name
    `package_weight`; bullets are one column each)."""
    return "package_weight" if key == GRAMS_COLUMN else key


def _shown(name: str, position: int) -> str:
    return name if _ECHO_SAFE.fullmatch(name) else f"column {position}"


def check_header(columns: Sequence[str]) -> list[tuple[ErrorCode, str]]:
    """Every header problem in column order, then the missing required columns; at most
    `MAX_ERROR_ITEMS` of them, the most a report holds (audit FILES-1)."""
    problems: list[tuple[ErrorCode, str]] = []

    def add(code: ErrorCode, message: str) -> None:
        if len(problems) < MAX_ERROR_ITEMS:
            problems.append((code, message))

    seen: dict[str, str] = {}
    present: set[str] = set()
    for position, raw in enumerate(columns, start=1):
        name = normalize_header(raw if isinstance(raw, str) else "")
        key = row_key(name)
        if key is None:
            add(ErrorCode.HEADER_UNKNOWN_COLUMN, f"unknown column {_shown(name, position)}")
            continue
        identity = name if key == "bullet_1_to_n" else _column_identity(key)
        if identity in seen:
            add(
                ErrorCode.HEADER_DUPLICATE_COLUMN,
                f"duplicate column {_shown(name, position)} (also given as {seen[identity]})",
            )
            continue
        seen[identity] = _shown(name, position)
        present.add(key)
    for required in REQUIRED:
        if required not in present:
            add(ErrorCode.HEADER_MISSING_COLUMN, f"missing column {required}")
    return problems


def header_report(problems: Sequence[tuple[ErrorCode, str]]) -> ErrorReport:
    if not problems:
        raise ValueError("header_report needs at least one problem")
    items = [
        ErrorItem(
            field=HEADER_FIELD,
            error_code=code,
            message=message,
            source=ErrorSource.SCHEMA,
            retryable=False,
        )
        for code, message in problems[:MAX_ERROR_ITEMS]
    ]
    return ErrorReport(
        status="failed", error_code=ErrorCode.IMPORT_FAILED, retryable=False, errors=items
    )
