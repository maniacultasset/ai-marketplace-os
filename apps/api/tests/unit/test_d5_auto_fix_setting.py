"""Client item 20 bullet 3 (12 Nov): "Let Kuinn fix safe listing errors automatically".

Off, every fix lane (an adapter repair, a content fix, an AI rewrite) is the seller's
`approve_fix`: the planner, the reads and the issue history all say "Needs you", and a held repair
shows as a suggested fix that waits. A plain retry stays automatic. The setting is owner only on
`PATCH /tenants/me` and never null."""

from __future__ import annotations

import dataclasses
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from app.api.v1.schemas.issue_history import FixOut, HistoryIssueOut
from app.api.v1.schemas.tenants import TenantOut, TenantPatch
from app.services import tenants
from app.services.resolution.apply_fixes import entry_for
from app.services.resolution.history import counts, fixes, issue_rows
from app.services.resolution.plan import (
    APPROVE_FIX,
    FIX_LANES,
    SELLER_ACTIONS,
    IssueView,
    PlanContext,
    label,
    plan,
)
from app.services.resolution.read import held_issues, labels, waiting_fixes
from pydantic import ValidationError

T0 = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)


def _issue(**over: Any) -> IssueView:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "source": "amazon_adapter",
        "field_path": "attributes.color",
        "error_code": "VALUE_NOT_ALLOWED",
        "error_class": "invalid_value",
        "severity": "error",
        "marketplace_code": "99300",
    }
    return IssueView(**{**base, **over})


# ---- the planner ----


def test_a_repair_is_the_sellers_to_approve_when_auto_fix_is_off() -> None:
    issue = _issue()
    on = plan([issue], PlanContext(repairable=frozenset({issue.id})))
    off = plan([issue], PlanContext(repairable=frozenset({issue.id}), auto_fix_enabled=False))
    assert [(s.lane, s.action) for s in on] == [("auto_repair", "repair")]
    assert [(s.lane, s.action) for s in off] == [("seller", APPROVE_FIX)]
    assert APPROVE_FIX in SELLER_ACTIONS  # no inbox question: the issue row is the signal


def test_content_and_ai_fixes_wait_for_the_seller_too() -> None:
    content = _issue(field_path="content.title", error_code="FIELD_TOO_LONG")
    ai = _issue(field_path="attributes.pattern", error_code="PATTERN_MISMATCH")
    ctx = PlanContext(content_fixable=frozenset({content.id}), ai_allowed=True)
    on = {s.issue_id: s.lane for s in plan([content, ai], ctx)}
    assert set(on.values()) <= set(FIX_LANES) | {"seller", "staff"}
    off = plan([content, ai], dataclasses.replace(ctx, auto_fix_enabled=False))
    for step in off:
        assert step.lane not in FIX_LANES
        if on[step.issue_id] in FIX_LANES:
            assert (step.lane, step.action) == ("seller", APPROVE_FIX)


def test_a_retry_stays_automatic() -> None:
    issue = _issue(error_code="MARKETPLACE_UNAVAILABLE", error_class="marketplace_technical")
    on = label(issue, PlanContext())
    off = label(issue, PlanContext(auto_fix_enabled=False))
    assert on is not None and off is not None and on[0] == off[0]


def test_human_lanes_do_not_change() -> None:
    issue = _issue(error_class="account_permission", error_code="CATEGORY_APPROVAL_REQUIRED")
    assert label(issue, PlanContext()) == label(issue, PlanContext(auto_fix_enabled=False))


# ---- the reads ----


@dataclass
class Row:
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    listing_id: uuid.UUID = field(default_factory=uuid.uuid4)
    source: str = "amazon_adapter"
    field_path: str = "attributes.color"
    error_code: str = "VALUE_NOT_ALLOWED"
    error_class: str = "invalid_value"
    severity: str = "error"
    status: str = "open"
    resolution: str | None = None
    action_taken: str | None = None
    human_task_id: uuid.UUID | None = None
    marketplace_code: str | None = "99300"
    dedupe_key: str = "k"
    recurrence_of_id: uuid.UUID | None = None
    message: str = "Amazon refused it."
    suggestion: str | None = None
    value_submitted: str | None = None
    occurrences: int = 1
    first_seen_at: datetime = T0
    last_seen_at: datetime = T0
    resolved_at: datetime | None = None


@dataclass
class Repair:
    source_issue_id: uuid.UUID | None
    held: bool = False
    rule: str = "enum_fold"
    field_path: str = "attributes.color"
    value_json: Any = field(default_factory=lambda: [{"value": "Navy", "marketplace_id": "X"}])
    created_at: datetime = T0
    retired_at: datetime | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)


def test_a_held_repair_is_the_sellers_and_a_live_one_kuinns() -> None:
    held, live = Row(), Row()
    repairs = [Repair(held.id, held=True), Repair(live.id)]
    lanes = labels(
        [held, live],
        repaired={r.source_issue_id for r in repairs if r.source_issue_id is not None},
        held=held_issues(repairs),
    )
    assert lanes[held.id][0] == "seller"
    assert lanes[live.id][0] == "auto_repair"


def test_held_issues_skip_retired_and_released_repairs() -> None:
    issue = uuid.uuid4()
    assert held_issues([Repair(issue, held=True)]) == frozenset({issue})
    assert held_issues([Repair(issue, held=True, retired_at=T0)]) == frozenset()
    assert held_issues([Repair(issue)]) == frozenset()


def test_a_held_repair_is_a_suggested_fix_that_waits() -> None:
    issue = Row()
    (fix,) = fixes(repairs=[Repair(issue.id, held=True)], issues=[], submissions=[])
    assert fix["suggested"] is True and fix["outcome"] == "waiting"
    FixOut.model_validate(fix)
    (made,) = fixes(repairs=[Repair(issue.id)], issues=[], submissions=[])
    assert made["suggested"] is False
    assert counts([], [fix, made])["fixed"] == 1  # a suggestion is not a fix Kuinn made


def _content_row(**over: Any) -> Row:
    base: dict[str, Any] = {"field_path": "content.title", "error_code": "FIELD_TOO_LONG"}
    return Row(**{**base, **over})


def test_a_suggested_content_fix_is_the_sellers_not_the_teams() -> None:
    """Auto-fix off, the executor keeps a content fix as `approve_fix` and opens no task: the
    reads must say the seller applies it, never that Kuinn's team is looking."""
    row = _content_row()
    assert labels([row])[row.id][0] == "staff"  # what the read said before (no task behind it)
    waiting = waiting_fixes([row])
    assert waiting == frozenset({row.id})
    assert labels([row], held=waiting)[row.id][0] == "seller"


def test_waiting_fixes_leave_out_rows_kuinn_or_a_person_already_has() -> None:
    tasked = _content_row(human_task_id=uuid.uuid4())
    repaired = _content_row()
    fixing = _content_row()
    closed = _content_row(status="resolved")
    warning = _content_row(severity="warning")
    own_check = _content_row(source="content_check")
    value = Row()  # an attribute value: a repair or a seller fact, never a content fix
    rows = [tasked, repaired, fixing, closed, warning, own_check, value]
    assert (
        waiting_fixes(rows, repaired={repaired.id}, fixing={fixing.id: "auto_content"})
        == frozenset()
    )


def test_a_locked_path_is_never_a_suggested_fix() -> None:
    row = _content_row()
    locked = {row.listing_id: frozenset({"content.title"})}
    assert waiting_fixes([row], locked=locked) == frozenset()


def test_the_history_marks_the_suggested_issue() -> None:
    row = _content_row(message="Amazon refused it. Marketplace message: too long")
    waiting = waiting_fixes([row])
    (out,) = issue_rows([row], labels([row], held=waiting), suggested=waiting)
    assert out["suggested"] is True and out["handler"] == "you"
    HistoryIssueOut.model_validate(out)
    (plain,) = issue_rows([row], labels([row]))
    assert plain["suggested"] is False


@pytest.mark.parametrize(
    ("status", "entry"),
    [("failed", "submission"), ("published", "read"), ("validated", "preview")],
)
def test_the_sellers_apply_runs_under_the_listing_status(status: str, entry: str) -> None:
    assert entry_for(status) == entry


# ---- the setting ----


def test_the_setting_is_on_by_default_and_never_null() -> None:
    out = TenantOut.model_validate(
        {
            "id": uuid.uuid4(),
            "name": "Fabrica",
            "slug": "fabrica",
            "status": "active",
        }
    )
    assert out.auto_fix_enabled is True
    assert TenantPatch.model_validate({"auto_fix_enabled": False}).auto_fix_enabled is False
    with pytest.raises(ValidationError):
        TenantPatch.model_validate({"auto_fix_enabled": None})
    with pytest.raises(ValidationError):
        TenantPatch.model_validate({"auto_fix_enabled": "no"})


def test_the_setting_is_owner_only_and_audited_on_its_own() -> None:
    assert frozenset({"owner"}) == tenants.SETTING_ROLES
    assert tenants.SETTINGS[tenants.AUTO_FIX_FIELD] == "tenant.auto_fix_enabled_changed"
    assert tenants.SETTINGS[tenants.SETTING_FIELD] == "tenant.owner_self_approval_changed"
