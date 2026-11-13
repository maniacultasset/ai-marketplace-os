"""Client pass 2, B13 review: rows stored before the plain-copy fix still carry an API path.
Every seller-facing issue read (the Inbox issue reads, the Home summary, the review, the issue
history) takes it out at read time with one helper."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from app.core.seller_text import seller_text, without_api_text

OLD = (
    "gender is required to list apparel on Amazon; set the product's gender "
    "(PATCH /api/v1/products/{product_id})."
)
API = ("/api/", "PATCH /", "POST /")


def _clean(text: str | None) -> bool:
    return text is not None and not any(a in text for a in API)


def test_the_stored_gender_finding_reads_plain() -> None:
    assert without_api_text(OLD) == (
        "gender is required to list apparel on Amazon; set the product's gender."
    )


@pytest.mark.parametrize(
    "text",
    ["Add the gender on the product page.", "Sleeve 3/4 , cotton", "POSTER size", "a / b"],
)
def test_plain_text_is_unchanged(text: str) -> None:
    assert without_api_text(text) == text


def test_optional_fields() -> None:
    assert seller_text(None) is None
    assert seller_text("(GET /api/v1/x)") is None
    assert seller_text("Call POST /tasks/1/answer now") == "Call now"


def test_home_blocked_text_strips_older_rows() -> None:
    from app.repositories.home import IssueRow, ListingRow
    from app.services.home import blocked_issue_text

    row = ListingRow(
        uuid.uuid4(), "amazon", "blocked", None, "not_listed", uuid.uuid4(), datetime.now(UTC),
        uuid.uuid4(), "SKU-1", "Kurta",
    )  # fmt: skip
    issue = IssueRow(uuid.uuid4(), row.listing_id, "MISSING_ATTRIBUTE", None, OLD, 1)
    assert _clean(blocked_issue_text(row, issue))


def _row(**over: Any) -> Any:
    names = (
        "id", "listing_id", "marketplace", "listing_item_id", "listing_version_id", "source",
        "field_path", "error_code", "error_class", "retry_class", "severity", "auto_fixable",
        "fix_action", "marketplace_code", "raw_message", "message", "plain_message", "suggestion",
        "value_submitted", "occurrences", "first_seen_at", "last_seen_at", "status",
        "resolution", "action_taken", "resolved_at", "resolved_by_user_id", "human_task_id",
        "recurrence_of_id", "created_at", "updated_at",
    )  # fmt: skip
    base: dict[str, Any] = dict.fromkeys(names)
    base.update(over)
    return SimpleNamespace(**base)


def test_issue_reads_strip_message_plain_message_and_suggestion() -> None:
    from app.services.issues import issue_fields

    got = issue_fields(
        _row(message=OLD, plain_message=OLD, suggestion="Run PATCH /api/v1/products/1")
    )
    assert _clean(got["message"]) and _clean(got["plain_message"])
    assert got["suggestion"] == "Run"
    assert issue_fields(_row(message="Title too long."))["message"] == "Title too long."


def test_issue_history_names_a_preflight_finding_as_kuinn_s() -> None:
    """A row from Kuinn's own adapter check (no marketplace code, no marketplace text) is not
    shown as the marketplace's words, and its stored API path is gone."""
    from app.services.resolution.history import issue_rows

    row = SimpleNamespace(
        id=uuid.uuid4(), message=OLD, field_path="facts.gender", error_code="MISSING_ATTRIBUTE",
        error_class="missing_data", severity="error", source="amazon_adapter", status="open",
        suggestion=None, value_submitted=None, marketplace_code=None, occurrences=1,
        first_seen_at=datetime.now(UTC), last_seen_at=datetime.now(UTC), resolved_at=None,
    )  # fmt: skip
    (got,) = issue_rows([row], {})
    assert got["marketplace_message"] is None
    assert _clean(got["cause"]) and got["cause"].startswith("gender is required")
