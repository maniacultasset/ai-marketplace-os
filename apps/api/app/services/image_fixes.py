"""Which candidate kinds the seller may ask for on one image (SI7, AC15): the `fixes` list of
`ProductImageOut`. Pure: reads the stored reports and the stored border profile only.

- not the `main` slot: none (CP-D1);
- no failed deterministic check, or a failed check that framing cannot fix (corruption, format,
  file size): none;
- the background passes: `fix` (framing only);
- the background fails on a uniform near-white border: `fix` (whitening is deterministic);
- otherwise `white_background`, only while the AI edit is switched on.

A row checked before SI7 has no border profile and counts as not near-white.

The SI14 photo QA checks (fill ratio, text on the image, watermark) are stored as deterministic
checks too, but they are warnings only (AI-43) and never decide a fix: a grey-background Front
with a "Low fill" warning still offers the white background (B2, chrome pass 1).

`fix_unavailable_for` names why a main photo whose checks failed offers no fix, so the page can
say the reason and the next step: `ai_off` (the white background needs the AI edit, switched
off) or `not_fixable` (a failed check no candidate can fix: corruption, format, file size).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final, Literal

__all__ = ["FIXABLE_CHECKS", "PHOTO_QA_CHECKS", "fix_unavailable_for", "fixes_for"]

FixKind = Literal["fix", "white_background"]
FixUnavailable = Literal["ai_off", "not_fixable"]
FIXABLE_CHECKS: Final = frozenset({"resolution", "aspect_ratio", "background"})
# SI14: warnings only, whatever the rule says (`image_checks._WARNING_ONLY`)
PHOTO_QA_CHECKS: Final = frozenset({"fill_ratio", "text_overlay", "watermark"})


def _failed(reports: Mapping[str, Any] | None) -> set[str]:
    """The failed deterministic checks over every stored marketplace report (no marketplace
    branch: invariant 4)."""
    failed: set[str] = set()
    for report in (reports or {}).values():
        if not isinstance(report, Mapping):
            continue
        for check in report.get("checks") or ():
            if (
                isinstance(check, Mapping)
                and check.get("method") == "deterministic"
                and check.get("passed") is False
            ):
                failed.add(str(check.get("check")))
    return failed - PHOTO_QA_CHECKS


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


def fix_unavailable_for(
    slot: str,
    reports: Mapping[str, Any] | None,
    border: Mapping[str, Any] | None,
    *,
    edit_enabled: bool,
) -> FixUnavailable | None:
    """Why a main photo with a failed check offers no fix; None when a fix is offered, nothing
    failed or the photo is not the main one."""
    if slot != "main":
        return None
    failed = _failed(reports)
    if not failed:
        return None
    if not failed <= FIXABLE_CHECKS:
        return "not_fixable"
    if fixes_for(slot, reports, border, edit_enabled=edit_enabled):
        return None
    return "ai_off"
