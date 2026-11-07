"""SI3 tester round 1: properties of `photo_facts.screen` over arbitrary pipeline output (pure).

Whatever the pipeline returns, the backend keeps only an allowed value for a target key, cited on
a product photo of this context, alone for its key; never writes a check or critical key; never
suggests an `ai` value for a piece count; opens at most `MAX_PHOTO_TASKS` questions, one per key;
and keeps nothing without a completed `photo_facts/*` record."""

from __future__ import annotations

from base64 import b64encode
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.core.resolved_facts import ProductFacts, ResolvedFact
from app.services import photo_facts as pf
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from mkos_schemas.agent import AgentRunRecord
from mkos_schemas.jobs import ContextImage, PhotoObservation, PhotoTarget

_JPEG = b64encode(b"\xff\xd8\xff\xe0" + b"\x00" * 16 + b"\xff\xd9").decode("ascii")
_TS = datetime(2026, 11, 7, tzinfo=UTC)
MAIN, SECOND, LABEL, CHART, FOREIGN = (UUID(int=n) for n in (1, 2, 3, 4, 5))
IMAGES = [
    ContextImage(image_id=MAIN, slot="main", width=8, height=8, data_b64=_JPEG),
    ContextImage(image_id=SECOND, slot="secondary", width=8, height=8, data_b64=_JPEG),
    ContextImage(image_id=LABEL, slot="packaging_label", width=8, height=8, data_b64=_JPEG),
    ContextImage(image_id=CHART, slot="size_chart", width=8, height=8, data_b64=_JPEG),
]
PHOTOS = {MAIN, SECOND}
KEYS = [*pf.V1_KEYS, "mrp", "fiber_composition", "construction", "material", "neck_line"]
JUNK = [
    "sparkly neck",
    "Round\u200bNeck",
    "\uff32\uff2f\uff35\uff2e\uff24 \uff2e\uff25\uff23\uff2b",
    "ignore previous instructions",
    "100% cotton",
    "ROUND NECK",
    "999",
]


def _record(**overrides: Any) -> AgentRunRecord:
    base: dict[str, Any] = {
        "id": UUID(int=99),
        "tenant_id": UUID(int=7),
        "request_id": "req-1",
        "agent_name": "image",
        "trigger": "vision",
        "prompt_version": "photo_facts/v1",
        "status": "completed",
        "started_at": _TS,
        "created_at": _TS,
    }
    base.update(overrides)
    return AgentRunRecord.model_validate(base)


@st.composite
def worlds(draw: st.DrawFn) -> dict[str, Any]:
    modes = draw(st.dictionaries(st.sampled_from(pf.V1_KEYS), st.sampled_from(["fill", "check"])))
    targets = [
        PhotoTarget(
            key=key,
            mode="check" if key in ("number_of_pieces",) else mode,
            allowed_values=list(pf.VOCAB[key][:6]),
            min_confidence=0.70,
        )
        for key, mode in modes.items()
    ]
    stored: dict[str, ResolvedFact] = {}
    for key in draw(st.sets(st.sampled_from(pf.V1_KEYS))):
        source = draw(st.sampled_from(["seller", "ai", "document"]))
        value = draw(st.sampled_from([*pf.VOCAB[key][:6], "Unmappable"]))
        confirmed = _TS.isoformat() if draw(st.booleans()) else None
        stored[key] = ResolvedFact(key=key, value=value, source=source, confirmed_at=confirmed)
    observations = []
    for _ in range(draw(st.integers(0, 16))):
        key = draw(st.sampled_from(KEYS))
        values = list(pf.VOCAB.get(key, ())[:8]) + JUNK
        observations.append(
            PhotoObservation(
                key=key,
                value=draw(st.sampled_from(values)),
                confidence=draw(st.floats(0, 1, allow_nan=False)),
                image_id=draw(st.sampled_from([MAIN, SECOND, LABEL, CHART, FOREIGN])),
            )
        )
    records = draw(
        st.sampled_from(
            [
                [_record()],
                [],
                [_record(status="failed")],
                [_record(prompt_version="writer/v3")],
                [_record(agent_name="content")],
            ]
        )
    )
    return {
        "targets": targets,
        "stored": stored,
        "observations": observations,
        "records": records,
        "required": frozenset(draw(st.sets(st.sampled_from(pf.V1_KEYS)))),
    }


@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(worlds())
def test_screen_keeps_only_what_the_rules_allow(w: dict[str, Any]) -> None:
    targets = {t.key: t for t in w["targets"]}
    decision = pf.screen(
        w["observations"],
        w["records"],
        targets=w["targets"],
        images=IMAGES,
        facts=ProductFacts(
            product_id=UUID(int=8), sku="S", level="standalone", attributes=w["stored"]
        ),
        required=w["required"],
        rejected={},
    )
    complete = w["records"] == [_record()]
    if not complete:
        assert decision.run_id is None and decision.writes == () and decision.tasks == ()
    counts: dict[str, int] = {}
    for o in w["observations"]:
        counts[o.key] = counts.get(o.key, 0) + 1
    for write in decision.writes:
        target = targets[write.key]
        assert target.mode == "fill"
        assert write.key not in pf.FORBIDDEN_KEYS and write.key != "number_of_pieces"
        assert write.value in target.allowed_values
        assert write.image_id in PHOTOS
        assert counts[write.key] == 1
        assert write.confidence >= target.min_confidence - 0.005
    assert len({w_.key for w_ in decision.writes}) == len(decision.writes)
    assert len(decision.tasks) <= pf.MAX_PHOTO_TASKS
    assert len({t.key for t in decision.tasks}) == len(decision.tasks)
    for task in decision.tasks:
        assert task.key in targets and counts[task.key] == 1
        assert task.image_id in PHOTOS
        assert task.kind in ("missing_fact", "conflicting_facts")
        ai = [s for s in task.suggestions if s["source"] == "ai"]
        if task.key == "number_of_pieces":
            assert ai == []
        for s in ai:
            assert s["value"] in targets[task.key].allowed_values
        assert len(task.suggestions) <= 10
        if task.kind == "conflicting_facts":
            # a conflict is only ever raised against a stored value the backend can map
            stored = w["stored"][task.key]
            assert pf.canonical(task.key, stored.resolved) is not None
    # every dropped entry names only a backend-known key or "-"
    for key, _reason in decision.dropped:
        assert key == "-" or key in pf.VOCAB or key in pf.FORBIDDEN_KEYS or key in targets
