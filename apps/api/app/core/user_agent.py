"""A coarse browser label from a User-Agent header ("Chrome on macOS"), for the trusted-browser
list (ADR-0023). Families only: no version, no device model, nothing that identifies a person.
Plain regexes, no dependency; anything unknown is "Browser" (at most 64 characters)."""

from __future__ import annotations

import re
from typing import Final

MAX_LABEL: Final = 64
_SCAN_MAX: Final = 512
FALLBACK: Final = "Browser"

# First match wins: the forks name themselves before the engine they share.
_BROWSERS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (re.compile(r"\bEdg(?:e|A|iOS)?/"), "Edge"),
    (re.compile(r"\b(?:OPR|Opera|OPX)/"), "Opera"),
    (re.compile(r"\bSamsungBrowser/"), "Samsung Internet"),
    (re.compile(r"\b(?:Firefox|FxiOS)/"), "Firefox"),
    (re.compile(r"\b(?:Chrome|CriOS|Chromium)/"), "Chrome"),
    (re.compile(r"\bVersion/[\d.]+.*\bSafari/"), "Safari"),
)
_SYSTEMS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (re.compile(r"\biPad\b"), "iPadOS"),
    (re.compile(r"\b(?:iPhone|iPod)\b"), "iOS"),
    (re.compile(r"\bCrOS\b"), "ChromeOS"),
    (re.compile(r"\bAndroid\b"), "Android"),
    (re.compile(r"\bWindows\b"), "Windows"),
    (re.compile(r"\bMac OS X\b|\bMacintosh\b"), "macOS"),
    (re.compile(r"\bLinux\b|\bX11\b"), "Linux"),
)


def _first(patterns: tuple[tuple[re.Pattern[str], str], ...], text: str) -> str | None:
    return next((name for pattern, name in patterns if pattern.search(text)), None)


def summary(user_agent: str | None) -> str:
    """ "<browser> on <system>", "<browser>", or "Browser" when neither is known."""
    if not user_agent:
        return FALLBACK
    text = user_agent[:_SCAN_MAX]
    browser = _first(_BROWSERS, text)
    system = _first(_SYSTEMS, text)
    if browser is None:
        return FALLBACK
    label = f"{browser} on {system}" if system else browser
    return label[:MAX_LABEL]


__all__ = ["FALLBACK", "MAX_LABEL", "summary"]
