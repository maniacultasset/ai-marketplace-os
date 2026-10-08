"""The image gate at approval (S17b T14; PLAN D3, D4; CP5; C8). Pure: `mkos_schemas` only.

`evaluate_image_gate(listing, stored, pipeline_reports)` judges each `listing.images[i]` for
`listing.marketplace`:

- no stored report (pending, unknown, deleted or another tenant's image) → one blocking item
  "image not checked yet" (`source=system`, retryable): an unchecked image never passes (D4);
- a stored report with `pass=false` → one blocking item per issue; with `pass=true` → one
  warning item per issue (the Pillow checks set `pass=false` only for an error-severity failure);
  these carry `source=rules_engine` and the issue code as `raw_code` (T84);
- the pipeline's reports for that image and marketplace add a warning item for each failed
  `vision` check, whatever their `pass` (C8: vision results never block in v1). Their
  deterministic checks are ignored: the stored report is the authority.

Every blocking item is `Severity.ERROR` and every warning `Severity.WARNING`, so an `ErrorReport`
built from both keeps only the gate's blocking items in `.blocking`. S19's `approve_batch` calls
this inside each item's savepoint (hand-off in KNOWN-ISSUES §E).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource, Severity
from mkos_schemas.errors import ErrorItem
from mkos_schemas.images import ImageIssue, ImageReport
from mkos_schemas.listing import CanonicalListing

NOT_CHECKED: Final = "image not checked yet"


@dataclass(frozen=True, slots=True)
class ImageGateResult:
    blocking: tuple[ErrorItem, ...]
    warnings: tuple[ErrorItem, ...]


def _item(
    path: str,
    *,
    severity: Severity,
    source: ErrorSource,
    message: str,
    listing: CanonicalListing,
    issue: ImageIssue | None = None,
    retryable: bool = False,
) -> ErrorItem:
    return ErrorItem(
        field=path,
        error_code=ErrorCode.IMAGE_NON_COMPLIANT,
        severity=severity,
        message=message,
        suggestion=None if issue is None else issue.suggestion,
        source=source,
        retryable=retryable,
        marketplace=listing.marketplace,
        raw_code=None if issue is None else issue.code.value,
    )


def evaluate_image_gate(
    listing: CanonicalListing,
    stored: Mapping[UUID, ImageReport],
    pipeline_reports: Sequence[ImageReport],
) -> ImageGateResult:
    blocking: list[ErrorItem] = []
    warnings: list[ErrorItem] = []
    market = listing.marketplace
    for i, ref in enumerate(listing.images):
        path = f"images[{i}]"
        report = stored.get(ref.image_id)
        if report is None or report.marketplace is not market or report.image_id != ref.image_id:
            blocking.append(
                _item(path, severity=Severity.ERROR, source=ErrorSource.SYSTEM,
                      message=NOT_CHECKED, listing=listing, retryable=True)
            )  # fmt: skip
        else:
            severity = Severity.WARNING if report.pass_ else Severity.ERROR
            target = warnings if report.pass_ else blocking
            for issue in report.issues:
                target.append(
                    _item(path, severity=severity, source=ErrorSource.RULES_ENGINE,
                          message=f"image check failed: {issue.check}", listing=listing,
                          issue=issue)
                )  # fmt: skip
        for vision in pipeline_reports:
            if vision.image_id != ref.image_id or vision.marketplace is not market:
                continue
            issues = {issue.check: issue for issue in vision.issues}
            for check in vision.checks:
                if check.method != "vision" or check.passed:
                    continue
                warnings.append(
                    _item(path, severity=Severity.WARNING, source=ErrorSource.VISION_MODEL,
                          message=f"vision check failed: {check.check}", listing=listing,
                          issue=issues.get(check.check))
                )  # fmt: skip
    return ImageGateResult(blocking=tuple(blocking), warnings=tuple(warnings))
