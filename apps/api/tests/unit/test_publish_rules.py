"""S21 T4 (AC2, AC3, AC5; CP9; DATABASE.md :862, :875, :1390-1398): the pure publish rules."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from app.services.publish_rules import (
    PreviousAttempt,
    choose_justification,
    choose_operation,
    mode_for,
    publish_event,
    report_retryable,
    submission_key,
)
from hypothesis import given
from hypothesis import strategies as st

NOW = datetime(2026, 10, 9, tzinfo=UTC)
VECTOR = "1dd1d44b7adf778649110db3b987b218458fb0a756525d108795161a0ee0755f"
STATUSES = (
    "created",
    "generating",
    "validated",
    "needs_review",
    "approved",
    "publishing",
    "published",
    "blocked",
    "failed",
)
REASONS = (
    None,
    "corrections_exhausted",
    "validation_errors",
    "rejected",
    "human_task_open",
    "marketplace_action_required",
    "publish_attempts_exhausted",
    "generation_failed",
    "publish_failed",
    "publish_partial",
    "marketplace_rejected",
)


def test_submission_key_vector() -> None:
    """hex sha256 of `tenant|listing|version|marketplace|attempt|payload_hex` (DATABASE.md:862)."""
    key = submission_key(
        uuid.UUID(int=1), uuid.UUID(int=2), uuid.UUID(int=3), "amazon", 1, bytes(range(32))
    )
    assert key == VECTOR


def test_submission_key_changes_with_attempt() -> None:
    args = (uuid.UUID(int=1), uuid.UUID(int=2), uuid.UUID(int=3), "amazon")
    assert submission_key(*args, 1, bytes(32)) != submission_key(*args, 2, bytes(32))


@pytest.mark.parametrize("attempt", [0, 4, True])
def test_submission_key_refuses_bad_attempt(attempt: Any) -> None:
    with pytest.raises(ValueError):
        submission_key(
            uuid.UUID(int=1), uuid.UUID(int=2), uuid.UUID(int=3), "amazon", attempt, bytes(32)
        )
    with pytest.raises(ValueError):
        submission_key(uuid.UUID(int=1), uuid.UUID(int=2), uuid.UUID(int=3), "amazon", 1, b"short")


@given(
    st.tuples(st.uuids(), st.uuids(), st.integers(1, 3), st.binary(min_size=32, max_size=32)),
    st.tuples(st.uuids(), st.uuids(), st.integers(1, 3), st.binary(min_size=32, max_size=32)),
)
def test_key_injective(a: tuple[Any, ...], b: tuple[Any, ...]) -> None:
    ka = submission_key(a[0], a[1], uuid.UUID(int=0), "amazon", a[2], a[3])
    kb = submission_key(b[0], b[1], uuid.UUID(int=0), "amazon", b[2], b[3])
    assert (ka == kb) == (a == b)


@pytest.mark.parametrize(
    ("accepted", "paths", "expected"),
    [(False, (), "create"), (True, (), "update"), (True, ("content.title",), "patch")],
)
def test_choose_operation(accepted: bool, paths: tuple[str, ...], expected: str) -> None:
    assert choose_operation(previously_accepted=accepted, retry_paths=paths) == expected


def _item(retryable: bool, severity: str = "error") -> dict[str, Any]:
    return {"field": "content.title", "severity": severity, "retryable": retryable}


@pytest.mark.parametrize(
    ("prev", "task", "moved", "expected"),
    [
        (PreviousAttempt("error", {"retryable": False}, NOW), True, False, "human_resolution"),
        (PreviousAttempt("error", {"retryable": True}, NOW), False, False, "retryable_condition"),
        (
            PreviousAttempt(
                "partially_accepted", {"retryable": True, "errors": [_item(True)]}, NOW
            ),
            False,
            False,
            "retryable_condition",
        ),
        (
            PreviousAttempt(
                "partially_accepted",
                {"retryable": True, "errors": [_item(True), _item(False)]},
                NOW,
            ),
            False,
            False,
            None,
        ),
        (
            PreviousAttempt("rejected", {"retryable": False}, NOW),
            False,
            True,
            "marketplace_transition",
        ),
        (PreviousAttempt("rejected", {"retryable": False}, NOW), False, False, None),
        (PreviousAttempt("error", {"retryable": True}, None), False, False, None),
        (PreviousAttempt("accepted", None, NOW), True, True, None),
    ],
    ids=[
        "human",
        "retryable_error",
        "retryable_partial",
        "non_retryable_partial",
        "transition",
        "none",
        "unresolved",
        "accepted",
    ],
)
def test_choose_justification(
    prev: PreviousAttempt, task: bool, moved: bool, expected: str | None
) -> None:
    assert (
        choose_justification(prev, applied_task_after=task, marketplace_changed_after=moved)
        == expected
    )


def test_report_retryable_rules() -> None:
    assert report_retryable({"retryable": True, "errors": [_item(True)]})
    assert report_retryable({"retryable": True, "errors": [_item(True), _item(False, "warning")]})
    assert not report_retryable({"retryable": True, "errors": [_item(False)]})
    assert not report_retryable({"retryable": False, "errors": [_item(True)]})
    assert not report_retryable({"retryable": True})
    assert not report_retryable(None)


def _expected_event(status: str, reason: str | None) -> str | None:
    if status == "approved":
        return "publish"
    if status == "failed" and reason in {
        "publish_failed",
        "publish_partial",
        "marketplace_rejected",
    }:
        return "retry_publish"
    if status == "blocked" and reason == "marketplace_action_required":
        return "resume_publish"
    return None


def test_publish_event_table() -> None:
    for status in STATUSES:
        for reason in REASONS:
            assert publish_event(status, reason) == _expected_event(status, reason), (
                status,
                reason,
            )


def test_partial_publish_is_retry_fields() -> None:
    assert mode_for("failed", "publish_partial") == "retry_fields"
    assert mode_for("failed", "publish_failed") == "publish"
    assert mode_for("blocked", "marketplace_action_required") == "resume"
    assert mode_for("approved", None) == "publish"


# ---- S26 T23 gate-fix (AC 11): the exact refusals and edges ----


def test_submission_key_refusal_messages() -> None:
    ids = (uuid.UUID(int=1), uuid.UUID(int=2), uuid.UUID(int=3), "amazon")
    with pytest.raises(ValueError) as refused:
        submission_key(*ids, 4, b"\x00" * 32)
    assert str(refused.value) == "attempt must be 1..3"
    with pytest.raises(ValueError) as refused:
        submission_key(*ids, 1, b"\x00" * 31)
    assert str(refused.value) == "payload_sha256 must be a 32-byte digest"


def test_report_retryable_item_without_severity_is_blocking() -> None:
    assert not report_retryable({"retryable": True, "errors": [{"retryable": False}]})
    assert report_retryable(
        {"retryable": True, "errors": [{"retryable": False, "severity": "warning"}]}
    )


def test_choose_justification_error_without_report() -> None:
    prev = PreviousAttempt("error", None, datetime(2026, 10, 1, tzinfo=UTC))
    assert (
        choose_justification(prev, applied_task_after=False, marketplace_changed_after=False)
        is None
    )
    assert (
        choose_justification(prev, applied_task_after=False, marketplace_changed_after=True)
        == "marketplace_transition"
    )


@pytest.mark.parametrize(
    ("status", "reason"),
    [("failed", "marketplace_action_required"), ("blocked", "publish_attempts_exhausted")],
)
def test_mode_for_resume_needs_blocked_and_the_action_reason(status: str, reason: str) -> None:
    assert mode_for(status, reason) == "publish"
    assert mode_for("blocked", "marketplace_action_required") == "resume"
