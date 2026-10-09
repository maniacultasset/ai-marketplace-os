"""Pure publish rules (S21 T4; DATABASE.md :862, :875, :1390-1398; PLAN D1, D2; CP9). No I/O.

- `submission_key`: the marketplace idempotency key of one submission, hex sha256 over the tenant,
  listing, version, marketplace, attempt and payload digest (DATABASE.md:862). A transport retry
  reuses the row and so the key; a new attempt gets a new key.
- `choose_operation`: `patch` for a field retry (the failed paths), `update` once a version of the
  listing was accepted, else `create`.
- `report_retryable`: an ErrorReport may be retried as is (`retryable_condition`) only when its
  flag is set **and** every blocking item is retryable: the same rule as `ls_guard` (0008).
- `choose_justification` (CP9): `human_resolution` (a task linked to an issue of the previous
  attempt was applied after it resolved), else `retryable_condition`, else
  `marketplace_transition`, else None (409 DUPLICATE_SUBMISSION_BLOCKED). v1 never writes
  `changed_input`: an edit makes a new version, which starts at attempt 1.
- `publish_event` / `mode_for`: the listing event a publish request writes, from the listing's
  status and reason (None = not publishable), and the job mode (D2: a publish of a
  `failed/publish_partial` listing is a field retry, never a whole re-push, L12033).
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal

from mkos_schemas.enums import Severity

Operation = Literal["create", "update", "patch"]
Justification = Literal["retryable_condition", "marketplace_transition", "human_resolution"]
PublishEvent = Literal["publish", "retry_publish", "resume_publish"]
Mode = Literal["publish", "retry_fields", "resume"]

RETRY_REASONS: Final = frozenset({"publish_failed", "publish_partial", "marketplace_rejected"})
RESUME_REASON: Final = "marketplace_action_required"
RETRYABLE_OUTCOMES: Final = frozenset({"partially_accepted", "rejected"})


def submission_key(
    tenant_id: uuid.UUID,
    listing_id: uuid.UUID,
    version_id: uuid.UUID,
    marketplace: str,
    attempt: int,
    payload_sha256: bytes,
) -> str:
    if not 1 <= attempt <= 3 or isinstance(attempt, bool):
        raise ValueError("attempt must be 1..3")
    if len(payload_sha256) != 32:
        raise ValueError("payload_sha256 must be a 32-byte digest")
    raw = "|".join(
        (
            str(tenant_id),
            str(listing_id),
            str(version_id),
            marketplace,
            str(attempt),
            payload_sha256.hex(),
        )
    )
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


def choose_operation(*, previously_accepted: bool, retry_paths: Sequence[str] = ()) -> Operation:
    if retry_paths:
        return "patch"
    return "update" if previously_accepted else "create"


def report_retryable(report: Mapping[str, Any] | None) -> bool:
    """The stored report's own flag and every blocking item retryable (0008 `ls_guard`)."""
    if not report or report.get("retryable") is not True:
        return False
    errors = report.get("errors")
    if not isinstance(errors, list):
        return False
    return all(
        item.get("retryable") is True
        for item in errors
        if isinstance(item, Mapping) and item.get("severity", Severity.ERROR.value) == "error"
    )


@dataclass(frozen=True, slots=True)
class PreviousAttempt:
    outcome: str
    error_report: Mapping[str, Any] | None
    resolved_at: datetime | None


def choose_justification(
    prev: PreviousAttempt, *, applied_task_after: bool, marketplace_changed_after: bool
) -> Justification | None:
    """CP9, for a previous attempt that is already resolved (a `pending` one is resolved first)."""
    if prev.resolved_at is None or prev.outcome not in {"error", *RETRYABLE_OUTCOMES}:
        return None
    if applied_task_after:
        return "human_resolution"
    if prev.outcome == "error":
        if prev.error_report is not None and prev.error_report.get("retryable") is True:
            return "retryable_condition"
    elif report_retryable(prev.error_report):
        return "retryable_condition"
    if marketplace_changed_after:
        return "marketplace_transition"
    return None


def publish_event(status: str, reason: str | None) -> PublishEvent | None:
    if status == "approved":
        return "publish"
    if status == "failed" and reason in RETRY_REASONS:
        return "retry_publish"
    if status == "blocked" and reason == RESUME_REASON:
        return "resume_publish"
    return None


def mode_for(status: str, reason: str | None) -> Mode:
    if status == "failed" and reason == "publish_partial":
        return "retry_fields"
    if status == "blocked" and reason == RESUME_REASON:
        return "resume"
    return "publish"
