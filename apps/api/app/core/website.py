"""The brand website rule, shared by the sign-up form, the brand profile API and the sign-up
approval (client item 1). Pure.

`WEBSITE_PATTERN` is the sign-up form's http(s) link pattern (ADR-0019); `WEBSITE_MAX` is the
`ck_brand_profiles_website_url` length (the form itself takes up to 2048). `profile_website(value)`
is a sign-up website fit for `brand_profiles.website_url`, else None.
"""

from __future__ import annotations

import re
from typing import Final

WEBSITE_PATTERN: Final = r"^https?://[^\s/?#\x00-\x1f\x7f]+\.[^\s\x00-\x1f\x7f]+$"
WEBSITE_MAX: Final = 2000
_WEBSITE: Final = re.compile(WEBSITE_PATTERN)


def profile_website(value: str | None) -> str | None:
    """`value` when it fits the brand profile column; None when it is missing, blank, longer
    than `WEBSITE_MAX`, holds a NUL or is not an http(s) link. Never raises."""
    if value is None:
        return None
    found = value.strip()
    if not found or len(found) > WEBSITE_MAX or "\x00" in found:
        return None
    return found if _WEBSITE.fullmatch(found) is not None else None
