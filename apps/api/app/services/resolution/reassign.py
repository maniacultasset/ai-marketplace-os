"""Amazon reassigning a listing's product type (SI13 T6; P:A1, D8; CP-D9, CP-D10). Pure: no I/O.

Amazon may file a listing under another product type than the one we sent (F:65, unconfirmed in
its docs, so treated as a risk), or start asking for a burst of attributes that belong to another
type. Either is a category problem the seller fixes by recategorising, never ten questions:

- `type_change`: the read's `summaries[0].productType` is set and differs from the product type of
  the version's pinned template;
- `burst`: the listing's SKU is known to Amazon (accepted before), this read carries at least
  `REASSIGN_BURST` MISSING_ATTRIBUTE ERROR items, and either fewer than that many such rows were
  open before this read (a new burst, not long-standing gaps) or the category item is already
  open (sticky while the burst lasts).

`category_item` is one ERROR `CATEGORY_MISMATCH` at `facts.category`, source the Amazon adapter, no
marketplace code, with a templated message. It is appended to the read's items, so SI11's
`close_missing` closes it once neither signal holds (CP-D10).
"""

from __future__ import annotations

import re
from typing import Final, Literal

from mkos_schemas.codes import ERROR_CODE_META, ErrorCode
from mkos_schemas.enums import ErrorSource, Marketplace, RetryClass, Severity
from mkos_schemas.errors import ErrorItem

Signal = Literal["type_change", "burst"]

REASSIGN_BURST: Final = 3
CATEGORY_PATH: Final = "facts.category"
PRODUCT_TYPE_RE: Final = re.compile(r"[A-Z0-9_]{1,64}", re.ASCII)
LABEL_RE: Final = re.compile(r"[a-z0-9_]{1,64}", re.ASCII)


def clean_type(value: object) -> str | None:
    """A product type safe to compare and echo (`^[A-Z0-9_]{1,64}$`), else None."""
    return value if isinstance(value, str) and PRODUCT_TYPE_RE.fullmatch(value) else None


def signal(
    read_type: str | None,
    sent_type: str | None,
    missing_now: int,
    accepted_before: bool,
    item_open: bool,
    missing_before: int = 0,
) -> Signal | None:
    read = clean_type(read_type)
    sent = clean_type(sent_type)
    if read is not None and sent is not None and read != sent:
        return "type_change"
    if not accepted_before or missing_now < REASSIGN_BURST:
        return None
    if item_open or missing_before < REASSIGN_BURST:
        return "burst"
    return None


def label_of(category: str) -> str | None:
    """The seller-facing name of a category key (`ethnic_wear_kurta_sets` -> `ethnic wear kurta
    sets`); None for anything that is not a plain key."""
    return category.replace("_", " ") if LABEL_RE.fullmatch(category) else None


def category_item(found: Signal, read_type: str | None, suggested: str | None) -> ErrorItem:
    shown = clean_type(read_type) if found == "type_change" else None
    if shown is not None:
        message = f"Amazon now files this product as {shown}."
    else:
        message = "Amazon now asks for the details of a different kind of product."
    message += " Choose the category that matches it, then regenerate the listing."
    label = label_of(suggested) if suggested else None
    if label is not None:
        message += f" Suggested: {label}."
    meta = ERROR_CODE_META[ErrorCode.CATEGORY_MISMATCH]
    return ErrorItem(
        field=CATEGORY_PATH,
        error_code=ErrorCode.CATEGORY_MISMATCH,
        severity=Severity.ERROR,
        message=message,
        source=ErrorSource.AMAZON_ADAPTER,
        retryable=meta.retry_class in (RetryClass.AUTO_RETRY, RetryClass.AI_CORRECT),
        marketplace=Marketplace.AMAZON,
        error_class=meta.error_class,
        retry_class=meta.retry_class,
    )
