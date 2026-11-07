"""Which candidate kinds the seller may ask for on one image (SI7, AC15): the `fixes` list of
`ProductImageOut`. Pure: reads the stored reports and the stored border profile only.

- not the `main` slot: none (CP-D1);
- no failed deterministic check, or a failed check that framing cannot fix (corruption, format,
  file size): none;
- the background passes: `fix` (framing only);
- the background fails on a uniform near-white border: `fix` (whitening is deterministic);
- otherwise `white_background`, only while the AI edit is switched on.

A row checked before SI7 has no border profile and counts as not near-white.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final, Literal

__all__ = ["FIXABLE_CHECKS", "fixes_for"]

FixKind = Literal["fix", "white_background"]
FIXABLE_CHECKS: Final = frozenset({"resolution", "aspect_ratio", "background"})
MARKETPLACES: Final = ("amazon", "flipkart")


def _failed(reports: Mapping[str, Any] | None) -> set[str]:
    failed: set[str] = set()
    for market in MARKETPLACES:
        report = (reports or {}).get(market)
        if not isinstance(report, Mapping):
            continue
        for check in report.get("checks") or ():
            if (
                isinstance(check, Mapping)
                and check.get("method") == "deterministic"
                and check.get("passed") is False
            ):
                failed.add(str(check.get("check")))
    return failed


def fixes_for(
    slot: str,
    reports: Mapping[str, Any] | None,
    border: Mapping[str, Any] | None,
    *,
    edit_enabled: bool,
) -> list[FixKind]:
    if slot != "main":
        return []
    failed = _failed(reports)
    if not failed or not failed <= FIXABLE_CHECKS:
        return []
    if "background" not in failed:
        return ["fix"]
    if border is not None and border.get("uniform") is True and border.get("near_white") is True:
        return ["fix"]
    return ["white_background"] if edit_enabled else []
