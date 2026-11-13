"""Seller text without API paths or HTTP verbs (client pass 2, B13).

The Amazon gender finding used to end "(PATCH /api/v1/products/{product_id})". New findings say
the next step in plain words; rows stored before that fix still carry the old text, so every
seller-facing issue read (the Inbox's issue reads, the Home summary) passes its text through
`without_api_text`. The marketplace's own words (`raw_message`) are left as they came.
"""

from __future__ import annotations

import re
from typing import Final

_VERB: Final = r"(?:GET|POST|PUT|PATCH|DELETE)"
_PATTERNS: Final = (
    re.compile(rf"\s*\(\s*{_VERB}\s+/[^)]*\)"),  # "(PATCH /api/v1/products/{product_id})"
    re.compile(r"\s*\(\s*/api/[^)]*\)"),  # "(/api/v1/...)"
    re.compile(rf"\s*\b{_VERB}\s+/\S*"),  # "PATCH /products/x"
    re.compile(r"\s*/api/\S*"),  # a bare "/api/v1/..."
)
_BEFORE_PUNCT: Final = re.compile(r"\s+([.,;:])")


def without_api_text(text: str) -> str:
    """`text` with any API path or HTTP verb taken out; unchanged when it has none."""
    out = text
    for pattern in _PATTERNS:
        out = pattern.sub("", out)
    return text if out == text else _BEFORE_PUNCT.sub(r"\1", out).strip()


def seller_text(text: str | None) -> str | None:
    """`without_api_text` for an optional field: None stays None, all-API text becomes None."""
    if text is None:
        return None
    return without_api_text(text) or None
