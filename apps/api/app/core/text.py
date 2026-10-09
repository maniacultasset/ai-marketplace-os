"""Free-text hygiene for request bodies (S18 fix round: NUL and C0 controls).

PostgreSQL `jsonb` and `text` refuse `\\u0000`, and other C0 control characters have no place in
listing copy or instructions. `\\n` and `\\t` stay allowed; `\\r` is refused (one line ending,
the plan's `\\n`/`\\t`-only rule). Kept API-side so the released contract (`mkos_schemas`) does not
change.
"""

from __future__ import annotations

import re
from typing import Any, Final

from pydantic_core import PydanticCustomError

_CONTROL: Final = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
CONTROL_CHAR_MESSAGE: Final = "Text may not contain control characters other than newline and tab."


def has_control_char(value: Any) -> bool:
    """True when any string in `value` (nested dicts and lists, keys included) has one."""
    if isinstance(value, str):
        return _CONTROL.search(value) is not None
    if isinstance(value, dict):
        return any(has_control_char(k) or has_control_char(v) for k, v in value.items())
    if isinstance(value, list | tuple):
        return any(has_control_char(item) for item in value)
    return False


def reject_control_chars(value: str) -> str:
    """An `AfterValidator` for one free-text string."""
    if has_control_char(value):
        raise PydanticCustomError("mkos_invalid", CONTROL_CHAR_MESSAGE)
    return value
