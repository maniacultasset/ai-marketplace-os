"""SI11 part B fix round 1 (B1-1, B1-4): the resolution read labels the rows Kuinn's content fix
or AI rewrite covers or closed with that lane (never staff), and a fix job that ended with no
version never reads `idle`."""

from __future__ import annotations

import uuid
from types import SimpleNamespace as N

from app.api.v1.schemas.resolution import ResolutionOut
from app.services.resolution.read import ReadFacts, fix_lanes, labels, next_step, state_of

from .test_state import Row


def _job(mode: str, issue_ids: list[uuid.UUID], status: str = "queued") -> N:
    return N(
        kind="listing_generate",
        status=status,
        payload_json={
            "trigger": "resolution",
            "mode": mode,
            "issue_ids": [str(i) for i in issue_ids],
        },
    )


def _content_row(**kw: object) -> Row:
    return Row(
        field_path="content.title",
        error_code="FIELD_TOO_LONG",
        error_class="invalid_value",
        marketplace_code="99301",
        **kw,  # type: ignore[arg-type]
    )


def test_an_open_row_a_content_fix_job_covers_reads_auto_content() -> None:
    row = _content_row()
    assert labels([row])[row.id][0] == "staff"  # the planner's own lane with no context
    fixing = fix_lanes([_job("auto_fix", [row.id])])
    assert labels([row], fixing=fixing)[row.id] == ("auto_content", "ai_content")


def test_an_open_row_an_ai_rewrite_covers_reads_ai_field() -> None:
    row = Row(field_path="content.description", error_code="BANNED_TERM")
    fixing = fix_lanes([_job("ai_field", [row.id], status="succeeded")])
    assert labels([row], fixing=fixing)[row.id][0] == "ai_field"


def test_a_failed_fix_job_covers_nothing() -> None:
    row = _content_row()
    assert fix_lanes([_job("auto_fix", [row.id], status="failed")]) == {}
    user_job = N(
        kind="listing_generate", status="queued", payload_json={"issue_ids": [str(row.id)]}
    )
    assert fix_lanes([user_job]) == {}


def test_rows_kuinn_closed_read_the_lane_that_closed_them() -> None:
    fixed = _content_row(status="auto_fixed", resolution="deterministic_fix", action_taken="trim")
    rewritten = Row(field_path="content.description", status="resolved", resolution="ai_correction")
    repaired = Row(status="auto_fixed", resolution="deterministic_fix", action_taken="repair:x")
    out = labels([fixed, rewritten, repaired])
    assert out[fixed.id] == ("auto_content", "ai_content")
    assert out[rewritten.id][0] == "ai_field"
    assert out[repaired.id][0] == "auto_repair"


def test_a_linked_task_beats_a_fix_job() -> None:
    task = uuid.uuid4()
    row = _content_row(human_task_id=task)
    out = labels(
        [row],
        task_kinds={task: "marketplace_error_review"},
        fixing=fix_lanes([_job("auto_fix", [row.id])]),
    )
    assert out[row.id][0] == "staff"


def test_a_failed_generation_with_an_open_error_is_stopped_with_regenerate() -> None:
    facts = ReadFacts(status="failed", status_reason="generation_failed", open_blocking=True)
    assert state_of(facts) == "stopped"
    step = next_step("stopped", facts)
    assert step["kind"] == "regenerate" and step["text"]
    ResolutionOut.model_validate({"state": "stopped", "cycles_used": 0, "next_step": step})


def test_a_failed_generation_with_a_seller_question_asks_the_seller() -> None:
    facts = ReadFacts(
        status="failed",
        status_reason="generation_failed",
        open_blocking=True,
        seller_tasks=(uuid.uuid4(),),
    )
    assert state_of(facts) == "needs_you"


def test_a_failed_generation_with_nothing_open_stays_idle() -> None:
    assert state_of(ReadFacts(status="failed", status_reason="generation_failed")) == "idle"
