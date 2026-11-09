"""SI11 T20 (AC18; CP-D18): the resolution read's pure parts: the state precedence, the next
step, an issue's lane and cause, and the attempt windows."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.api.v1.schemas.resolution import ResolutionOut
from app.repositories.resolution import LinkedTask, VersionRow
from app.services.resolution.read import (
    ReadFacts,
    attempts,
    labels,
    next_step,
    state_of,
)

T = uuid.uuid4
NOW = datetime(2026, 11, 8, 9, 0, tzinfo=UTC)

# one fixture per state, each the minimum that reaches it
STATES: dict[str, ReadFacts] = {
    "live": ReadFacts(status="published"),
    "fixing": ReadFacts(status="generating", live_fix_job=True),
    "checking": ReadFacts(status="validated", remote_check_status="pending", live_status_sync=True),
    "resubmitted": ReadFacts(status="publishing", latest_submission=("system", "pending")),
    "stopped": ReadFacts(status="blocked", status_reason="corrections_exhausted"),
    "needs_marketplace": ReadFacts(
        status="blocked", status_reason="marketplace_action_required", marketplace_tasks=(T(),)
    ),
    "needs_you": ReadFacts(status="blocked", seller_tasks=(T(),)),
    "fix_ready": ReadFacts(status="needs_review", fix_ready=True),
    "idle": ReadFacts(status="validated"),
}
ORDER = list(STATES)


@pytest.mark.parametrize("state", ORDER)
def test_each_state_has_its_fixture(state: str) -> None:
    assert state_of(STATES[state]) == state


def _merge(a: ReadFacts, b: ReadFacts) -> ReadFacts:
    """Both fixtures' signals at once (the higher one keeps its status)."""
    base = ReadFacts(status="")
    out = a
    for name in ReadFacts.__slots__:
        if name in ("status", "status_reason"):
            continue
        if getattr(out, name) == getattr(base, name):
            out = replace(out, **{name: getattr(b, name)})
    return out


@pytest.mark.parametrize(
    ("high", "low"), [(h, lo) for i, h in enumerate(ORDER) for lo in ORDER[i + 1 :] if lo != "idle"]
)
def test_the_higher_state_wins_every_pair(high: str, low: str) -> None:
    assert state_of(_merge(STATES[high], STATES[low])) == high


def test_a_pending_stamp_with_no_live_status_sync_job_is_not_checking() -> None:
    """Fix round 2 (B2-1): a `pending` stamp left behind with no job never reads `checking`."""
    orphan = ReadFacts(
        status="failed",
        status_reason="generation_failed",
        remote_check_status="pending",
        open_blocking=True,
    )
    assert state_of(orphan) == "stopped"
    assert next_step("stopped", orphan)["kind"] == "regenerate"
    assert state_of(ReadFacts(status="validated", remote_check_status="pending")) == "idle"
    assert state_of(replace(orphan, live_status_sync=True)) == "checking"
    assert state_of(ReadFacts(status="validated", live_preview=True)) == "checking"


def test_an_empty_listing_is_idle() -> None:
    assert state_of(ReadFacts(status="draft")) == "idle"


def test_three_cycles_with_an_open_error_is_stopped() -> None:
    assert state_of(ReadFacts(status="blocked", cycles=3, open_blocking=True)) == "stopped"
    assert state_of(ReadFacts(status="blocked", cycles=3)) == "idle"
    assert state_of(ReadFacts(status="blocked", cycles=2, open_blocking=True)) == "idle"


def test_a_settled_resend_is_not_resubmitted() -> None:
    for outcome in ("rejected", "error", "live", "superseded"):
        assert state_of(ReadFacts(status="failed", latest_submission=("system", outcome))) == "idle"
    assert state_of(ReadFacts(status="publishing", latest_submission=("user", "pending"))) == "idle"


def test_a_marketplace_block_without_its_task_is_not_needs_marketplace() -> None:
    facts = ReadFacts(status="blocked", status_reason="marketplace_action_required")
    assert state_of(facts) == "idle"


def test_a_published_listing_with_a_waiting_repair_says_it_goes_out_next() -> None:
    facts = ReadFacts(status="published", waiting_repairs=("attributes.color",))
    step = next_step(state_of(facts), facts)
    assert step == {
        "kind": "none",
        "text": "Kuinn's fix for attributes.color goes out with the next update.",
    }


@pytest.mark.parametrize("state", ORDER)
def test_every_state_gives_a_valid_response(state: str) -> None:
    facts = STATES[state]
    ResolutionOut.model_validate(
        {"state": state, "cycles_used": 3, "attempts": [], "next_step": next_step(state, facts)}
    )


def test_cycles_used_is_bounded_by_the_model() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ResolutionOut.model_validate(
            {"state": "idle", "cycles_used": 4, "next_step": {"kind": "none"}}
        )


# ---- labels ----


@dataclass
class Row:
    field_path: str = "attributes.color"
    error_code: str = "VALUE_NOT_ALLOWED"
    error_class: str = "invalid_value"
    severity: str = "error"
    source: str = "amazon_adapter"
    marketplace_code: str | None = "99300"
    dedupe_key: str = "k"
    action_taken: str | None = None
    human_task_id: uuid.UUID | None = None
    id: uuid.UUID = None  # type: ignore[assignment]
    status: str = "open"
    resolution: str | None = None
    resolved_at: datetime | None = None
    first_seen_at: datetime = NOW
    listing_version_id: uuid.UUID | None = None
    plain_message: str | None = None
    message: str = "The value is not allowed."

    def __post_init__(self) -> None:
        if self.id is None:
            self.id = uuid.uuid4()


def test_a_repaired_row_is_auto_repair() -> None:
    row = Row()
    assert labels([row], repaired=[row.id])[row.id][0] == "auto_repair"
    closed = Row(status="auto_fixed", action_taken="repair:enum_fold")
    assert labels([closed])[closed.id] == ("auto_repair", "seller_fact")


@pytest.mark.parametrize(
    ("kind", "lane"),
    [
        ("permission_decision", "marketplace"),
        ("marketplace_error_review", "staff"),
        ("missing_fact", "seller"),
        ("image_replacement", "seller"),
    ],
)
def test_a_linked_task_names_the_lane(kind: str, lane: str) -> None:
    task = uuid.uuid4()
    row = Row(human_task_id=task)
    assert labels([row], task_kinds={task: kind})[row.id][0] == lane


def test_an_unlinked_row_takes_the_planner_lane() -> None:
    unknown = Row(
        field_path="listing", error_code="MARKETPLACE_UNKNOWN_ERROR", error_class="unknown"
    )
    qual = Row(field_path="listing", marketplace_code="99320")
    out = labels([unknown, qual])
    assert out[unknown.id] == ("staff", "mapping")
    assert out[qual.id] == ("marketplace", "marketplace")


def test_a_warning_carries_no_lane() -> None:
    assert labels([Row(severity="warning")]) == {}


# ---- attempts ----


@dataclass
class Job:
    queued_at: datetime
    payload_json: dict[str, Any]
    result_json: dict[str, Any] | None = None
    status: str = "succeeded"
    kind: str = "listing_status_sync"
    finished_at: datetime | None = None
    id: uuid.UUID = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.id = self.id or uuid.uuid4()


@dataclass
class Sub:
    created_at: datetime
    listing_version_id: uuid.UUID
    attempt: int
    actor_type: str
    outcome: str


@dataclass
class Repair:
    created_at: datetime
    field_path: str
    attribute: str
    rule: str


def test_attempts_newest_first_with_their_window() -> None:
    v = uuid.uuid4()
    versions = {v: VersionRow(v, 2, "generation", None, None)}
    pre = Job(NOW, {"op": "preview", "version_id": str(v)}, {"remote_check": "issues"})
    sub = Sub(NOW + timedelta(minutes=5), v, 1, "system", "rejected")
    pre_row = Row(
        dedupe_key="a|preview", first_seen_at=NOW + timedelta(seconds=1), listing_version_id=v
    )
    live_row = Row(dedupe_key="b", first_seen_at=NOW + timedelta(minutes=6), field_path="listing")
    repair = Repair(NOW + timedelta(seconds=2), "attributes.color", "color", "enum_fold")
    task = uuid.uuid4()
    answered = LinkedTask(
        pre_row.id,
        task,
        "missing_fact",
        "applied",
        "facts.attributes.color",
        NOW + timedelta(minutes=1),
    )
    out = attempts(
        jobs=[pre],
        submissions=[sub],
        issues=[pre_row, live_row],
        repairs=[repair],
        tasks=[answered],
        versions=versions,
        lanes={},
    )
    assert [(a["kind"], a["number"], a["version"], a["actor"], a["outcome"]) for a in out] == [
        ("submission", 1, 2, "system", "rejected"),
        ("precheck", 0, 2, "system", "issues"),
    ]
    assert [f["field"] for f in out[0]["failed"]] == ["listing"]
    assert [f["field"] for f in out[1]["failed"]] == ["attributes.color"]
    assert [(c["kind"], c["field"]) for c in out[1]["changed"]] == [
        ("repair", "attributes.color"),
        ("seller_answer", "facts.attributes.color"),
    ]
    assert out[0]["changed"] == []


def test_a_live_preview_is_pending_and_a_lost_one_failed() -> None:
    v = uuid.uuid4()
    versions = {v: VersionRow(v, 1, "generation", None, None)}
    live = Job(NOW, {"op": "preview", "version_id": str(v)}, None, status="queued")
    lost = Job(
        NOW - timedelta(hours=1), {"op": "preview", "version_id": str(v)}, None, status="failed"
    )
    sync = Job(NOW, {}, None)  # a plain status read is no attempt
    out = attempts(
        jobs=[live, lost, sync],
        submissions=[],
        issues=[],
        repairs=[],
        tasks=[],
        versions=versions,
        lanes={},
    )
    assert [a["outcome"] for a in out] == ["pending", "failed"]


def test_at_most_ten_attempts() -> None:
    v = uuid.uuid4()
    versions = {v: VersionRow(v, 1, "generation", None, None)}
    jobs = [
        Job(
            NOW + timedelta(minutes=i),
            {"op": "preview", "version_id": str(v)},
            {"remote_check": "clean"},
        )
        for i in range(14)
    ]
    out = attempts(
        jobs=jobs, submissions=[], issues=[], repairs=[], tasks=[], versions=versions, lanes={}
    )
    assert len(out) == 10
    assert out[0]["at"] == NOW + timedelta(minutes=13)


def test_a_superseded_submission_is_an_attempt() -> None:
    v = uuid.uuid4()
    versions = {v: VersionRow(v, 1, "generation", None, None)}
    out = attempts(
        jobs=[],
        submissions=[Sub(NOW, v, 1, "user", "superseded")],
        issues=[],
        repairs=[],
        tasks=[],
        versions=versions,
        lanes={},
    )
    assert out[0]["outcome"] == "superseded" and out[0]["actor"] == "user"


# ---- SI13 (CP-D8): seller actions with no inbox question ----


@pytest.mark.parametrize("action", ["recategorize", "edit_identifier"])
def test_a_seller_action_reads_needs_you_with_its_kind(action: str) -> None:
    from app.api.v1.schemas.resolution import NextStepOut

    facts = ReadFacts(status="failed", status_reason="marketplace_rejected", seller_action=action)
    assert state_of(facts) == "needs_you"
    step = next_step("needs_you", facts)
    assert step["kind"] == action and step["text"]
    assert NextStepOut.model_validate(step).kind == action


def test_an_open_question_wins_over_a_seller_action() -> None:
    task = T()
    facts = ReadFacts(status="blocked", seller_tasks=(task,), seller_action="recategorize")
    assert next_step("needs_you", facts)["kind"] == "answer"


def test_seller_action_picks_recategorize_first() -> None:
    from app.services.resolution.read import seller_action

    @dataclass
    class Row:
        id: uuid.UUID
        error_code: str
        status: str = "open"
        severity: str = "error"

    cat, ean, other = Row(T(), "CATEGORY_MISMATCH"), Row(T(), "IDENTIFIER_DUPLICATE"), Row(T(), "X")
    lanes: dict[uuid.UUID, Any] = {
        cat.id: ("seller", "mapping"),
        ean.id: ("seller", "seller_fact"),
        other.id: ("seller", "seller_fact"),
    }
    assert seller_action([ean, cat, other], lanes) == "recategorize"
    assert seller_action([ean], lanes) == "edit_identifier"
    assert seller_action([ean], {ean.id: ("staff", "mapping")}) is None


def test_next_step_kind_accepts_only_the_two_new_members() -> None:
    from app.api.v1.schemas.resolution import NextStepOut
    from pydantic import ValidationError

    for kind in ("recategorize", "edit_identifier"):
        assert NextStepOut(kind=kind).kind == kind
    with pytest.raises(ValidationError):
        NextStepOut(kind="rename_sku")
