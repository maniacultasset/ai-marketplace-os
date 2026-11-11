"""V1.1 (client feedback item 9): the pure parts of the variant photo match. The key, the seller's
question, the stored value's read, the gate's unresolved verdicts, what a finished check stores,
the answer's trust checks and the seam's mirror of the pipeline contract."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from app.services.image_gate import MATCH_MESSAGES, evaluate_image_gate
from app.services.photo_match import (
    CHECKING_STALE,
    colour_of,
    job_of,
    match_key,
    question_for,
    read_state,
    state_fields,
    unresolved,
)
from app.workers.photo_match import _checked, _Read, stored_value
from app.workers.photo_match_seam import PhotoMatchAnswer, PhotoMatchRequest
from mkos_schemas.agent import AgentRunRecord, TokenUsage
from mkos_schemas.enums import ErrorSource, ImageSlot, Marketplace, Severity
from mkos_schemas.images import ImageReport
from mkos_schemas.listing import CanonicalListing

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
IMAGE = uuid.UUID("70000000-0000-4000-8000-00000000000a")
TENANT = uuid.UUID("70000000-0000-4000-8000-000000000001")
PRODUCT = uuid.UUID("70000000-0000-4000-8000-000000000002")
JOB = uuid.UUID("70000000-0000-4000-8000-000000000003")
HASH = "a" * 64


def _stored(**extra: Any) -> dict[str, Any]:
    return {
        "status": "mismatch",
        "key": "k" * 64,
        "source_hash": HASH,
        "colour": "Green",
        "job_id": str(JOB),
        "queued_at": (NOW - timedelta(minutes=1)).isoformat(),
        "checked_at": NOW.isoformat(),
        "product_match": 0.93,
        "colour_match": 0.05,
        "seen_colour": "red",
        "reason": "The photo shows a red kurta.",
        "question": "Is this photo the Green Cotton Kurta?",
        **extra,
    }


# ---- the key and the question ----


def test_the_key_is_per_image_content_and_colour() -> None:
    key = match_key(IMAGE, HASH, "Bottle Green")
    assert len(key) == 64
    assert match_key(IMAGE, HASH, "  bottle   GREEN ") == key  # spacing and case fold
    assert match_key(IMAGE, HASH, "Red") != key
    assert match_key(IMAGE, "b" * 64, "Bottle Green") != key
    assert match_key(uuid.uuid4(), HASH, "Bottle Green") != key
    assert match_key(IMAGE, HASH, None) == match_key(IMAGE, HASH, "")


def test_the_question_names_the_colour_and_the_product() -> None:
    assert question_for("Green", "Cotton  Kurta") == "Is this photo the Green Cotton Kurta?"
    assert question_for(None, "Brass Table Lamp") == "Is this photo the Brass Table Lamp?"
    long = question_for("Red", "Women Rayon " + "very " * 30 + "long kurta")
    assert long.endswith("…?") and len(long) < 100


def test_colour_of_reads_the_colour_fact() -> None:
    assert colour_of({"color": {"value": "  Navy   Blue ", "source": "seller"}}) == "Navy Blue"
    assert colour_of({"color": {"value": ""}}) is None
    assert colour_of({"color": "Navy"}) is None
    assert colour_of(None) is None


# ---- the stored value as the read sees it ----


def test_read_state_reads_a_stored_mismatch() -> None:
    state = read_state(_stored(), clean_hash=HASH, now=NOW)
    assert state is not None
    assert (state.status, state.colour, state.product_match, state.colour_match) == (
        "mismatch",
        "Green",
        0.93,
        0.05,
    )
    assert state.question == "Is this photo the Green Cotton Kurta?" and not state.kept
    fields = state_fields(state)
    assert fields["status"] == "mismatch" and fields["kept"] is False
    assert fields["kept_by_user_id"] is None


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "match",
        {"status": "great", "source_hash": HASH},
        {"status": "match"},  # no hash: made for unknown pixels
        {"status": "match", "source_hash": "b" * 64},  # other pixels
    ],
)
def test_read_state_drops_what_it_cannot_trust(raw: Any) -> None:
    assert read_state(raw, clean_hash=HASH, now=NOW) is None


def test_no_clean_copy_reads_as_no_match() -> None:
    assert read_state(_stored(), clean_hash=None, now=NOW) is None


def test_a_check_left_running_reads_as_not_checked() -> None:
    running = _stored(status="checking", question=None)
    assert read_state(running, clean_hash=HASH, now=NOW).status == "checking"  # type: ignore[union-attr]
    late = NOW + CHECKING_STALE + timedelta(seconds=1)
    assert read_state(running, clean_hash=HASH, now=late).status == "failed"  # type: ignore[union-attr]
    assert read_state(
        _stored(status="checking", queued_at="x"), clean_hash=HASH, now=NOW
    ).status == (  # type: ignore[union-attr]
        "failed"
    )


def test_a_check_reads_by_its_job_when_the_job_was_looked_up() -> None:
    running = _stored(status="checking", question=None)
    # queued far longer than the clock fallback: still running while its job is live
    late = NOW + CHECKING_STALE * 3
    state = read_state(running, clean_hash=HASH, now=late, live={JOB})
    assert state is not None and state.status == "checking"
    # the job ended without storing a verdict (dead-lettered, cancelled): not checked, at once
    state = read_state(running, clean_hash=HASH, now=NOW, live=set())
    assert state is not None and state.status == "failed"
    state = read_state(running, clean_hash=HASH, now=NOW, live={uuid.uuid4()})
    assert state is not None and state.status == "failed"
    # no job id on the stored check: not checked
    state = read_state(_stored(status="checking", job_id="x"), clean_hash=HASH, now=NOW, live={JOB})
    assert state is not None and state.status == "failed"
    # a verdict is never touched by the job lookup
    assert read_state(_stored(), clean_hash=HASH, now=NOW, live=set()).status == "mismatch"  # type: ignore[union-attr]


def test_job_of_names_only_a_running_check() -> None:
    assert job_of(_stored(status="checking")) == JOB
    assert job_of(_stored(status="match")) is None
    assert job_of(_stored(status="checking", job_id=None)) is None
    assert job_of(None) is None


def test_out_of_range_scores_and_odd_text_are_dropped() -> None:
    state = read_state(
        _stored(product_match=1.4, colour_match=True, reason=7, seen_colour=""),
        clean_hash=HASH,
        now=NOW,
    )
    assert state is not None
    assert state.product_match is None and state.colour_match is None
    assert state.reason is None and state.seen_colour is None


def test_a_match_carries_no_question_and_no_keep() -> None:
    state = read_state(
        _stored(status="match", kept_at=NOW.isoformat(), kept_by=str(TENANT)),
        clean_hash=HASH,
        now=NOW,
    )
    assert state is not None and state.question is None and not state.kept


def test_kept_records_who_and_when() -> None:
    user = uuid.uuid4()
    state = read_state(
        _stored(kept_at=NOW.isoformat(), kept_by=str(user)), clean_hash=HASH, now=NOW
    )
    assert state is not None and state.kept and state.kept_by == user and state.kept_at == NOW


# ---- the gate ----


@pytest.mark.parametrize(
    ("status", "kept", "want"),
    [
        ("mismatch", False, "mismatch"),
        ("unsure", False, "unsure"),
        ("mismatch", True, "kept_mismatch"),
        ("unsure", True, None),
        ("match", False, None),
        ("failed", False, None),
        ("checking", False, "checking"),
    ],
)
def test_unresolved(status: str, kept: bool, want: str | None) -> None:
    extra: dict[str, Any] = {"status": status}
    if kept:
        extra |= {"kept_at": NOW.isoformat(), "kept_by": str(uuid.uuid4())}
    assert unresolved(read_state(_stored(**extra), clean_hash=HASH, now=NOW)) == want
    assert unresolved(None) is None


def _listing(*ids: uuid.UUID) -> CanonicalListing:
    return CanonicalListing.model_validate(
        {
            "product_id": str(PRODUCT),
            "marketplace": "amazon",
            "category": "kurtas",
            "content": {"title": "A kurta", "bullets": ["one"], "description": "d"},
            "images": [
                {"image_id": str(i), "slot": "main" if n == 0 else "secondary"}
                for n, i in enumerate(ids)
            ],
        }
    )


def _passed(image_id: uuid.UUID) -> ImageReport:
    return ImageReport(
        image_id=image_id,
        marketplace=Marketplace.AMAZON,
        slot=ImageSlot.MAIN,
        pass_=True,
        checks=[],
        issues=[],
        processed_at=NOW,
    )


def test_an_unresolved_mismatch_blocks_and_unsure_warns() -> None:
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    stored = {i: _passed(i) for i in (a, b, c)}
    result = evaluate_image_gate(_listing(a, b, c), stored, [], {b: "mismatch", c: "unsure"})
    assert [(i.field, i.severity) for i in result.blocking] == [("images[1]", Severity.ERROR)]
    assert [(i.field, i.severity) for i in result.warnings] == [("images[2]", Severity.WARNING)]
    assert result.blocking[0].source is ErrorSource.VISION_MODEL
    assert result.blocking[0].message == MATCH_MESSAGES["mismatch"]
    assert result.warnings[0].message == MATCH_MESSAGES["unsure"]


def test_a_running_check_and_a_kept_mismatch_warn_and_never_block() -> None:
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    stored = {i: _passed(i) for i in (a, b, c)}
    result = evaluate_image_gate(_listing(a, b, c), stored, [], {b: "checking", c: "kept_mismatch"})
    assert result.blocking == ()
    assert [(i.field, i.message, i.source) for i in result.warnings] == [
        ("images[1]", MATCH_MESSAGES["checking"], ErrorSource.VISION_MODEL),
        ("images[2]", MATCH_MESSAGES["kept_mismatch"], ErrorSource.VISION_MODEL),
    ]
    assert all(i.severity is Severity.WARNING for i in result.warnings)


def test_without_matches_the_gate_is_unchanged() -> None:
    a = uuid.uuid4()
    plain = evaluate_image_gate(_listing(a), {a: _passed(a)}, [])
    assert evaluate_image_gate(_listing(a), {a: _passed(a)}, [], {}) == plain
    assert plain.blocking == () and plain.warnings == ()
    # a verdict for an image the listing does not use changes nothing
    assert (
        evaluate_image_gate(_listing(a), {a: _passed(a)}, [], {uuid.uuid4(): "mismatch"}) == plain
    )


# ---- what a finished check stores ----


def _read(colour: str | None = "Green") -> _Read:
    from app.repositories.images import ListingImageRow

    row = ListingImageRow(
        image_id=IMAGE,
        product_id=PRODUCT,
        slot="secondary",
        colourway_id=None,
        position=0,
        clean_content_hash=HASH,
        vision={"w": 64, "h": 64},
        colourway_position=None,
        created_at=NOW,
    )
    return _Read(
        image=row,
        reference=None,
        product_id=PRODUCT,
        title="Cotton Kurta",
        category="ethnic_wear_kurta_sets",
        colour=colour,
        source_hash=HASH,
        claim_started_at=NOW - timedelta(minutes=1),
    )


def test_stored_value_of_each_verdict() -> None:
    mismatch = PhotoMatchAnswer(
        status="ok", verdict="mismatch", product_match=0.93, colour_match=0.05, seen_colour="red"
    )
    value = stored_value(mismatch, _read(), NOW)
    assert value["status"] == "mismatch" and value["question"] == (
        "Is this photo the Green Cotton Kurta?"
    )
    assert value["checked_at"] == NOW.isoformat() and value["colour_match"] == 0.05
    match = replace_answer(mismatch, verdict="match", colour_match=0.9)
    assert stored_value(match, _read(), NOW)["question"] is None
    failed = PhotoMatchAnswer(status="failed")
    assert stored_value(failed, _read(), NOW) == {"status": "failed", "checked_at": NOW.isoformat()}


def replace_answer(answer: PhotoMatchAnswer, **update: Any) -> PhotoMatchAnswer:
    return answer.model_copy(update=update)


# ---- the answer's trust checks ----


class _Ctx:
    tenant_id = TENANT
    job_run_id = JOB
    request_id = "req-1"
    attempt = 1


def _record(**extra: Any) -> AgentRunRecord:
    body: dict[str, Any] = {
        "id": uuid.uuid4(),
        "tenant_id": TENANT,
        "product_id": PRODUCT,
        "request_id": "req-1",
        "job_id": JOB,
        "agent_name": "image",
        "trigger": "vision",
        "attempt": 1,
        "model_used": "gpt-4o-mini",
        "prompt_version": "photo_match/v1+abc",
        "tokens": TokenUsage(),
        "cost_usd": Decimal("0.001"),
        "status": "completed",
        "input_digest": "d" * 64,
        "started_at": NOW - timedelta(seconds=5),
        "created_at": NOW,
    }
    return AgentRunRecord(**(body | extra))


def _answer(**extra: Any) -> PhotoMatchAnswer:
    body: dict[str, Any] = {
        "status": "ok",
        "verdict": "match",
        "product_match": 0.95,
        "colour_match": 0.9,
        "agent_runs": [_record()],
    }
    return PhotoMatchAnswer(**(body | extra))


def test_a_trusted_answer_is_restamped_from_the_job() -> None:
    checked = _checked(_Ctx(), _read(), _answer())  # type: ignore[arg-type]
    assert isinstance(checked, PhotoMatchAnswer)
    assert checked.agent_runs[0].request_id == "req-1" and checked.agent_runs[0].job_id == JOB


@pytest.mark.parametrize(
    ("answer", "check"),
    [
        (object(), "type"),
        (_answer(verdict=None), "verdict"),
        (_answer(product_match=None), "verdict"),
        (_answer(agent_runs=[_record(job_id=uuid.uuid4())]), "identity"),
        (_answer(agent_runs=[_record(agent_name="content")]), "agent"),
        (_answer(agent_runs=[_record(trigger="generate")]), "trigger"),
        (_answer(agent_runs=[_record(prompt_version="photo_facts/v3+x")]), "prompt_version"),
    ],
)
def test_an_untrusted_answer_names_its_check(answer: Any, check: str) -> None:
    assert _checked(_Ctx(), _read(), answer) == check  # type: ignore[arg-type]


def test_a_newer_pipeline_answer_with_extra_keys_is_read() -> None:
    class Newer:
        def model_dump_json(self) -> str:
            return _answer().model_dump_json()[:-1] + ', "added_in_0_9": 1}'

    assert isinstance(_checked(_Ctx(), _read(), Newer()), PhotoMatchAnswer)  # type: ignore[arg-type]


def test_an_out_of_range_score_is_refused() -> None:
    class Bad:
        def model_dump_json(self) -> str:
            return (
                _answer().model_dump_json().replace('"product_match":0.95', '"product_match":1.5')
            )

    assert _checked(_Ctx(), _read(), Bad()) == "schema"  # type: ignore[arg-type]


# ---- the seam mirror of the pipeline contract ----


def test_the_request_mirrors_the_pipeline_contract() -> None:
    fields = set(PhotoMatchRequest.model_fields)
    assert fields == {
        "schema_version",
        "tenant_id",
        "product_id",
        "job_id",
        "request_id",
        "image",
        "reference",
        "title",
        "category",
        "colour",
    }
    assert PhotoMatchRequest.model_config.get("hide_input_in_errors") is True
    assert set(PhotoMatchAnswer.model_fields) >= {
        "status",
        "verdict",
        "product_match",
        "colour_match",
        "seen_colour",
        "reason",
        "agent_runs",
        "error",
    }


# ---- the staff requeue ----


async def test_staff_cannot_requeue_a_photo_match_job() -> None:
    from types import SimpleNamespace

    from app.repositories.jobs import NotEligibleError
    from app.services.image_candidate_hooks import on_image_requeue

    old = SimpleNamespace(entity_type="photo_match", id=JOB)
    with pytest.raises(NotEligibleError):
        await on_image_requeue(None, old, old)  # type: ignore[arg-type]
