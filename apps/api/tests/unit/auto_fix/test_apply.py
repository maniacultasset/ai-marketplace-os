"""SI4 A4 (AC4, AC7): the fix pass keeps a fix only when the engine says it helped and broke
nothing, and it is deterministic and idempotent."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from app.services.auto_fix import apply
from app.services.auto_fix.apply import MAX_FIXES_PER_PASS, auto_fix
from app.services.auto_fix.fixers import FixContext, fix_reformat, write_slot
from app.services.auto_fix.text import ACTION_MAX, ACTION_MIN
from app.services.rules_engine import evaluate
from hypothesis import given, settings
from hypothesis import strategies as st
from mkos_schemas.codes import ErrorCode
from mkos_schemas.errors import ErrorReport
from mkos_schemas.listing import CanonicalListing
from tests.rules_engine_builders import listing, rule, ruleset, template

UNIT = "Cotton Kurta Set for Women with Straight Fit and Round Neck "
STUB_212 = (UNIT * 5)[:211] + "."
TITLE_RULE = UUID("51400000-0000-0000-0000-00000000b001")
BANNED_RULE = UUID("51400000-0000-0000-0000-00000000b002")
MIN_RULE = UUID("51400000-0000-0000-0000-00000000b003")
CRITICAL = frozenset({"fiber_composition"})


def _title_rule(limit: int = 200) -> Any:
    return rule({"type": "max_length", "limit": limit}, id=TITLE_RULE, field="content.title")


def _banned() -> Any:
    return rule(
        {"type": "banned_terms", "terms": ["forbidden"]},
        id=BANNED_RULE,
        field="content.description",
    )


def _ctx(*rules: Any, tmpl: Any = None) -> FixContext:
    return FixContext(
        ruleset=ruleset(*(rules or (_title_rule(), _banned()))),
        template=tmpl,
        critical_keys=CRITICAL,
    )


class Engine:
    """The real engine, counting its calls."""

    def __init__(self, ctx: FixContext) -> None:
        self.ctx = ctx
        self.calls = 0

    def __call__(self, item: CanonicalListing) -> ErrorReport:
        self.calls += 1
        return evaluate(item, self.ctx.ruleset, template=self.ctx.template)


def _codes(report: ErrorReport) -> list[ErrorCode]:
    return [e.error_code for e in report.errors]


def test_a_long_title_is_fixed_and_recorded() -> None:
    ctx = _ctx()
    engine = Engine(ctx)
    out = auto_fix(listing(content={"title": STUB_212}), engine, ctx)
    assert len(out.listing.content.title) <= 200
    assert ErrorCode.FIELD_TOO_LONG not in _codes(out.report)
    assert out.report.passed
    assert len(out.records) == 1
    assert out.records[0].field == "content.title"
    assert out.records[0].rule_id == TITLE_RULE


def test_a_fix_that_breaks_another_rule_is_discarded(monkeypatch: pytest.MonkeyPatch) -> None:
    def breaking(item: CanonicalListing, error: Any, ctx: FixContext) -> Any:
        real = apply_real(item, error, ctx)
        assert real is not None
        fixed, record = real
        return write_slot(fixed, "content.description", "now forbidden here"), record

    apply_real = fix_reformat
    monkeypatch.setattr(apply, "fix_reformat", breaking)
    ctx = _ctx()
    base = listing(content={"title": STUB_212})
    out = auto_fix(base, Engine(ctx), ctx)
    assert out.listing == base
    assert out.records == ()
    assert ErrorCode.FIELD_TOO_LONG in _codes(out.report)
    assert ErrorCode.BANNED_TERM not in _codes(out.report)


def test_a_fix_that_leaves_its_item_is_discarded(monkeypatch: pytest.MonkeyPatch) -> None:
    def useless(item: CanonicalListing, error: Any, ctx: FixContext) -> Any:
        real = apply_real(item, error, ctx)
        assert real is not None
        return write_slot(item, "content.title", STUB_212 + " more"), real[1]

    apply_real = fix_reformat
    monkeypatch.setattr(apply, "fix_reformat", useless)
    ctx = _ctx()
    base = listing(content={"title": STUB_212})
    out = auto_fix(base, Engine(ctx), ctx)
    assert out.listing == base
    assert out.records == ()


def test_a_trim_under_a_min_length_rule_is_discarded() -> None:
    min_rule = rule({"type": "min_length", "limit": 201}, id=MIN_RULE, field="content.title")
    ctx = _ctx(_title_rule(), min_rule)
    base = listing(content={"title": STUB_212})
    out = auto_fix(base, Engine(ctx), ctx)
    assert out.listing == base
    assert out.records == ()
    assert _codes(out.report) == [ErrorCode.FIELD_TOO_LONG]


def test_no_deterministic_item_is_one_evaluation() -> None:
    ctx = _ctx()
    engine = Engine(ctx)
    base = listing(content={"description": "a forbidden word"})
    out = auto_fix(base, engine, ctx)
    assert out.listing is base
    assert out.records == ()
    assert engine.calls == 1
    assert _codes(out.report) == [ErrorCode.BANNED_TERM]


def _many_enum_keys(count: int) -> tuple[CanonicalListing, Any]:
    keys = [f"key_{i:02d}" for i in range(count)]
    spec = {"requirement": "optional", "value_type": "enum", "allowed_values": ["Straight Fit"]}
    tmpl = template(specs=dict.fromkeys(keys, spec))
    return listing(attributes=dict.fromkeys(keys, "straight-fit")), tmpl


def test_at_most_max_fixes_per_pass() -> None:
    base, tmpl = _many_enum_keys(MAX_FIXES_PER_PASS + 1)
    ctx = _ctx(tmpl=tmpl)
    out = auto_fix(base, Engine(ctx), ctx)
    assert len(out.records) == MAX_FIXES_PER_PASS == 20
    assert _codes(out.report) == [ErrorCode.VALUE_NOT_ALLOWED]


def test_deterministic_and_idempotent() -> None:
    base, tmpl = _many_enum_keys(3)
    base = base.model_copy(update={"content": base.content.model_copy(update={"title": STUB_212})})
    ctx = _ctx(_title_rule(), _banned(), tmpl=tmpl)
    first = auto_fix(base, Engine(ctx), ctx)
    second = auto_fix(base, Engine(ctx), ctx)
    assert first.listing.model_dump(mode="json") == second.listing.model_dump(mode="json")
    assert first.records == second.records
    assert len(first.records) == 4
    assert [r.field for r in first.records] == sorted(r.field for r in first.records)
    again = auto_fix(first.listing, Engine(ctx), ctx)
    assert again.records == ()
    assert again.listing == first.listing


def test_a_critical_key_is_never_attempted(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def spy(item: CanonicalListing, error: Any, ctx: FixContext) -> Any:
        calls.append(error.field)
        return None

    monkeypatch.setattr(apply, "fix_remap_value", spy)
    spec = {"requirement": "optional", "value_type": "enum", "allowed_values": ["Cotton"]}
    tmpl = template(specs={"fiber_composition": spec, "fit": spec})
    base = listing(attributes={"fiber_composition": "cotton-", "fit": "cotton-"})
    ctx = _ctx(tmpl=tmpl)
    auto_fix(base, Engine(ctx), ctx)
    assert calls == ["attributes.fit"]


_WORD = st.sampled_from(["Cotton", "Kurta", "Set", "Women", "Straight", "Fit", "कुर्ता", "Neck"])


@settings(max_examples=60, deadline=None)
@given(words=st.lists(_WORD, min_size=40, max_size=48))
def test_action_taken_never_holds_slot_text(words: list[str]) -> None:
    title = " ".join(words)
    ctx = _ctx(_title_rule(int(len(title) * 0.9)))
    out = auto_fix(listing(content={"title": title}), Engine(ctx), ctx)
    for record in out.records:
        assert title not in record.action_taken
        assert out.listing.content.title not in record.action_taken
        assert ACTION_MIN <= len(record.action_taken) <= ACTION_MAX


def test_an_ai_fit_phrase_is_remapped_to_the_kurta_enum() -> None:
    """Prod QA 2026-11-09 item 24: an AI "regular fit" against Amazon's Regular / Relaxed / Slim."""
    from app.services.auto_fix.fixers import _one_spelling

    fit = ["Regular", "Relaxed", "Slim"]
    assert _one_spelling("regular fit", fit) == "Regular"
    assert _one_spelling("Slim Fit", fit) == "Slim"
    assert _one_spelling("loose fit", fit) is None
