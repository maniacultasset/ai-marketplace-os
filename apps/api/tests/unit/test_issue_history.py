"""Feedback item 20: the issue history read's pure parts (`services/resolution/history.py`)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.api.v1.schemas.issue_history import IssueHistoryOut
from app.repositories.resolution import FixVersion
from app.services.resolution.history import (
    AI_FIX_REASON,
    LEFT_OUT,
    OTHER_PHOTO,
    REPAIR_FALLBACK,
    REPAIR_REASONS,
    UNMAPPED_CAUSE,
    after_value,
    attempt_rows,
    counts,
    fixes,
    handler_of,
    issue_rows,
    outcome_after,
    render_value,
    split_message,
    value_at,
)

T0 = datetime(2026, 11, 11, 10, 0, tzinfo=UTC)


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


@dataclass
class Issue:
    message: str = "Amazon does not accept it. Marketplace message: Bad colour."
    field_path: str = "attributes.color"
    error_code: str = "VALUE_NOT_ALLOWED"
    error_class: str = "invalid_value"
    severity: str = "error"
    source: str = "amazon_adapter"
    status: str = "open"
    resolution: str | None = None
    action_taken: str | None = None
    suggestion: str | None = "Pick a value Amazon allows."
    value_submitted: str | None = "Navy blue"
    marketplace_code: str | None = "90244"
    occurrences: int = 1
    first_seen_at: datetime = T0
    last_seen_at: datetime = T0
    resolved_at: datetime | None = None
    listing_version_id: uuid.UUID | None = None
    dedupe_key: str = "amazon_adapter|attributes.color|VALUE_NOT_ALLOWED|90244"
    id: uuid.UUID = field(default_factory=uuid.uuid4)


@dataclass
class Repair:
    source_issue_id: uuid.UUID | None
    rule: str = "enum_fold"
    field_path: str = "attributes.color"
    value_json: Any = field(default_factory=lambda: [{"value": "Navy", "marketplace_id": "X"}])
    created_at: datetime = field(default_factory=lambda: at(1))
    retired_at: datetime | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)


@dataclass
class Sub:
    created_at: datetime
    outcome: str
    listing_version_id: uuid.UUID
    actor_type: str = "system"


# ---- split_message, handler_of ----


def test_split_message_mapped() -> None:
    assert split_message("Plain. Marketplace message: Raw.") == ("Plain.", "Raw.")


def test_split_message_unmapped_keeps_the_raw_text() -> None:
    assert split_message("Raw only.") == (UNMAPPED_CAUSE, "Raw only.")
    assert split_message("") == (UNMAPPED_CAUSE, None)
    assert split_message(" Marketplace message: Raw.") == (UNMAPPED_CAUSE, "Raw.")


@pytest.mark.parametrize(
    ("lane", "klass", "outside", "want"),
    [
        ("auto_repair", "invalid_value", False, "kuinn"),
        ("auto_content", "content_error", False, "kuinn"),
        ("ai_field", "content_error", False, "kuinn"),
        ("retry", "marketplace_technical", False, "kuinn"),
        ("staff", "marketplace_technical", False, "marketplace"),
        (None, "marketplace_technical", False, "marketplace"),
        ("marketplace", "invalid_value", False, "marketplace"),
        ("seller", "invalid_value", False, "you"),
        ("seller", "missing_data", True, "marketplace"),
        ("staff", "missing_data", True, "marketplace"),
        ("marketplace", "account_permission", False, "marketplace"),
        ("staff", "unknown", False, "staff"),
        (None, "unknown", False, "staff"),
    ],
)
def test_handler_of(lane: Any, klass: str, outside: bool, want: str) -> None:
    assert handler_of(lane, error_class=klass, outside=outside) == want


# ---- render_value ----


@pytest.mark.parametrize(
    ("value", "want"),
    [
        (None, None),
        ([{"value": "Navy", "marketplace_id": "A21"}], "Navy"),
        ([{"value": 12.5, "unit": "centimeters"}], "12.5 centimeters"),
        ([{"value": {"value": 3, "unit": "kg"}}], "3 kg"),
        ([{"value": True}], "yes"),
        ([{"value": "S"}, {"value": "M"}], "S, M"),
        (
            [{"media_location": "https://x.test/a.jpg", "marketplace_id": "A21"}],
            "https://x.test/a.jpg",
        ),
        ("8901001000018", "8901001000018"),
        ([{"nested": {"a": 1}}], '[{"nested":{"a":1}}]'),
    ],
)
def test_render_value(value: Any, want: str | None) -> None:
    assert render_value(value) == want


def test_render_value_is_cut() -> None:
    got = render_value([{"value": "x" * 900}])
    assert got is not None and len(got) == 500


# ---- outcome_after ----


def test_outcome_after_reads_the_first_send_after_the_change() -> None:
    v = uuid.uuid4()
    subs = [Sub(at(5), "rejected", v), Sub(at(2), "accepted", v), Sub(at(-1), "rejected", v)]
    assert outcome_after(at(1), subs) == "accepted"
    assert outcome_after(at(3), subs) == "refused"
    assert outcome_after(at(9), subs) == "waiting"
    assert outcome_after(at(1), subs, retired=True) == "replaced"
    assert outcome_after(at(0), [Sub(at(1), "pending", v)]) == "sent"
    assert outcome_after(at(0), [Sub(at(1), "superseded", v)]) == "replaced"
    assert outcome_after(at(0), [Sub(at(1), "live", v)]) == "accepted"
    assert outcome_after(at(0), [Sub(at(1), "error", v)]) == "refused"


# ---- issue_rows ----


def test_issue_rows_marketplace_errors_only_newest_first() -> None:
    old = Issue(last_seen_at=at(0))
    new = Issue(last_seen_at=at(5), field_path="content.title")
    warning = Issue(severity="warning")
    ours = Issue(source="rules_engine")
    rows = issue_rows([old, warning, ours, new], {old.id: ("seller", "seller_fact")})
    assert [r["id"] for r in rows] == [new.id, old.id]
    first, second = rows
    assert second["handler"] == "you" and second["lane"] == "seller"
    assert first["handler"] == "staff" and first["lane"] is None
    assert second["cause"] == "Amazon does not accept it."
    assert second["marketplace_message"] == "Bad colour."
    assert second["group"] == "content_data"


def test_issue_rows_outside_pair_moves_to_the_marketplace() -> None:
    row = Issue(
        message="Flipkart needs the tax details. Marketplace message: tax missing",
        suggestion="Add them in Flipkart Seller Hub.",
        error_class="missing_data",
        source="flipkart_adapter",
    )
    outside = frozenset({("Flipkart needs the tax details.", "Add them in Flipkart Seller Hub.")})
    (got,) = issue_rows([row], {row.id: ("staff", "mapping")}, outside)
    assert got["handler"] == "marketplace" and got["group"] == "missing_attribute"
    (kept,) = issue_rows([row], {row.id: ("staff", "mapping")}, frozenset())
    assert kept["handler"] == "staff"


def test_issue_rows_cap_and_clip() -> None:
    rows = [Issue(last_seen_at=at(i), marketplace_code="9" * 90) for i in range(130)]
    got = issue_rows(rows, {})
    assert len(got) == 100
    assert all(len(r["marketplace_code"]) == 64 for r in got)


# ---- fixes ----


def test_a_repair_fix_has_before_after_reason_and_outcome() -> None:
    issue = Issue()
    repair = Repair(source_issue_id=issue.id)
    v = uuid.uuid4()
    (fix,) = fixes(repairs=[repair], issues=[issue], submissions=[Sub(at(2), "accepted", v)])
    assert (fix["kind"], fix["before"], fix["after"]) == ("repair", "Navy blue", "Navy")
    assert fix["reason"] == REPAIR_REASONS["enum_fold"]
    assert fix["outcome"] == "accepted" and fix["issue_id"] == issue.id


@pytest.mark.parametrize(
    ("rule", "value", "after"),
    [
        ("drop_locked", None, LEFT_OUT),
        ("image_slot", [{"media_location": "https://x"}], OTHER_PHOTO),
    ],
)
def test_repair_after_never_shows_a_url_or_null_for_special_rules(
    rule: str, value: Any, after: str
) -> None:
    (fix,) = fixes(repairs=[Repair(None, rule=rule, value_json=value)], issues=[], submissions=[])
    assert fix["after"] == after and fix["before"] is None and fix["outcome"] == "waiting"


def test_unknown_rule_reads_the_fallback_reason() -> None:
    (fix,) = fixes(repairs=[Repair(None, rule="future_rule")], issues=[], submissions=[])
    assert fix["reason"] == REPAIR_FALLBACK


def test_retired_repair_reads_replaced() -> None:
    (fix,) = fixes(repairs=[Repair(None, retired_at=at(3))], issues=[], submissions=[])
    assert fix["outcome"] == "replaced"


def test_content_and_ai_fixes_come_from_closed_issues() -> None:
    content = Issue(
        resolution="deterministic_fix", status="auto_fixed", resolved_at=at(2),
        action_taken="Kuinn cut the title to 200 characters.", field_path="content.title",
    )  # fmt: skip
    ai = Issue(resolution="ai_correction", status="resolved", resolved_at=at(3))
    repaired = Issue(resolution="deterministic_fix", resolved_at=at(4), action_taken="repair:x")
    still_open = Issue(resolution="ai_correction", resolved_at=None)
    versions = [
        _version(at(2), ("content.title",), {"content": {"title": "Short title"}}),
        _version(at(3), ("attributes.color",), {"attributes": {"color": "Navy"}}),
        _version(at(9), ("content.title",), {"content": {"title": "A later edit"}}),
    ]
    got = fixes(
        repairs=[], issues=[content, ai, repaired, still_open], submissions=[], versions=versions
    )
    assert [f["id"] for f in got] == [ai.id, content.id]  # newest first
    assert got[0]["kind"] == "ai_correction" and got[0]["reason"] == AI_FIX_REASON
    assert got[0]["after"] == "Navy"
    assert got[1]["kind"] == "content_fix"
    assert got[1]["reason"] == "Kuinn cut the title to 200 characters."
    assert got[1]["before"] == "Navy blue" and got[1]["after"] == "Short title"


def test_content_fix_without_its_version_has_no_after() -> None:
    row = Issue(resolution="deterministic_fix", resolved_at=at(2), field_path="content.title")
    (fix,) = fixes(repairs=[], issues=[row], submissions=[])
    assert fix["after"] is None


def _version(when: datetime, paths: tuple[str, ...], content: dict[str, Any]) -> FixVersion:
    return FixVersion(uuid.uuid4(), when, paths, content)


@pytest.mark.parametrize(
    ("path", "want"),
    [
        ("content.title", "T"),
        ("content.bullets[1]", "b2"),
        ("content.bullets[7]", None),
        ("content.bullets", ["b1", "b2"]),
        ("attributes.weight", {"value": 3, "unit": "kg"}),
        ("attributes.missing", None),
        ("content.title.deeper", None),
        ("Bad Path", None),
    ],
)
def test_value_at(path: str, want: Any) -> None:
    content = {
        "content": {"title": "T", "bullets": ["b1", "b2"]},
        "attributes": {"weight": {"value": 3, "unit": "kg"}},
    }
    assert value_at(content, path) == want


def test_after_value_picks_the_closing_version() -> None:
    old = _version(at(1), ("content.bullets",), {"content": {"bullets": ["old"]}})
    new = _version(at(2), ("content.bullets",), {"content": {"bullets": ["new"]}})
    other = _version(at(2), ("content.title",), {"content": {"title": "x", "bullets": ["no"]}})
    later = _version(at(5), ("content.bullets",), {"content": {"bullets": ["later"]}})
    assert after_value("content.bullets[0]", at(2), [old, new, other, later]) == "new"
    assert after_value("content.bullets[0]", at(0), [old, new]) is None
    weight = _version(
        at(1), ("attributes.weight",), {"attributes": {"weight": {"value": 3, "unit": "kg"}}}
    )
    assert after_value("attributes.weight", at(1), [weight]) == "3 kg"


# ---- counts and attempts ----


def test_counts_only_open_rows_by_handler() -> None:
    rows = [
        {"status": "open", "handler": "kuinn"},
        {"status": "open", "handler": "you"},
        {"status": "open", "handler": "you"},
        {"status": "resolved", "handler": "you"},
        {"status": "open", "handler": "marketplace"},
        {"status": "open", "handler": "staff"},
    ]
    assert counts(rows, [{}, {}]) == {
        "fixed": 2, "fixing": 1, "needs_you": 2, "marketplace": 1, "staff": 1,
    }  # fmt: skip


@dataclass
class Version:
    version: int


def test_attempt_rows_put_issues_and_fixes_in_their_window() -> None:
    v1 = uuid.uuid4()
    subs = [Sub(at(10), "accepted", v1), Sub(at(0), "rejected", v1)]
    first: dict[str, Any] = {"id": uuid.uuid4(), "first_seen_at": at(1)}
    preview: dict[str, Any] = {"id": uuid.uuid4(), "first_seen_at": at(2)}
    channels = {first["id"]: ("live", v1), preview["id"]: ("preview", v1)}
    fixed = [{"id": uuid.uuid4(), "at": at(3)}, {"id": uuid.uuid4(), "at": at(11)}]
    got = attempt_rows(
        jobs=[],
        submissions=subs,
        total_submissions=None,
        versions={v1: Version(1)},
        issues=[first, preview],
        channels=channels,
        fixed=fixed,
    )
    assert [(a["number"], a["outcome"]) for a in got] == [(2, "accepted"), (1, "rejected")]
    assert got[1]["issue_ids"] == [first["id"]]  # the preview row is not a send's
    assert got[1]["fix_ids"] == [fixed[0]["id"]]
    assert got[0]["issue_ids"] == [] and got[0]["fix_ids"] == [fixed[1]["id"]]


def test_the_wire_model_accepts_the_built_shape() -> None:
    issue = Issue()
    rows = issue_rows([issue], {issue.id: ("auto_repair", "mapping")})
    made = fixes(repairs=[Repair(issue.id)], issues=[issue], submissions=[])
    out = IssueHistoryOut.model_validate(
        {"marketplace": "amazon", "counts": counts(rows, made), "issues": rows, "fixes": made}
    )
    assert out.issues[0].handler == "kuinn" and out.fixes[0].after == "Navy"
    assert out.counts.fixing == 1 and out.counts.fixed == 1
