"""S14a T13: Hypothesis properties of the Rules Engine (AC 8; D13, D14)."""

from __future__ import annotations

from typing import Any

from app.services.rules_engine import evaluate
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from mkos_schemas.enums import Severity
from mkos_schemas.rules import Rule
from tests.rules_engine_builders import ruleset
from tests.rules_engine_strategies import (
    accounts,
    listings,
    products,
    rule_lists,
    templates,
    tenant_rule_lists,
)

PROPERTY = settings(
    derandomize=True,
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)


def _run(
    item: Any, rules: list[Rule], tpl: Any = None, product: Any = None, account: Any = None
) -> Any:
    return evaluate(item, ruleset(*rules), template=tpl, product=product, account=account)


@PROPERTY
@given(listings(), rule_lists(), templates, products, accounts)
def test_rules_engine_properties_deterministic(
    item: Any, rules: list[Rule], tpl: Any, product: Any, account: Any
) -> None:
    first = _run(item, rules, tpl, product, account).model_dump_json()
    second = _run(item, rules, tpl, product, account).model_dump_json()
    assert first == second


@PROPERTY
@given(st.data(), listings(), rule_lists(), templates)
def test_rules_engine_properties_shuffle_invariant(
    data: st.DataObject, item: Any, rules: list[Rule], tpl: Any
) -> None:
    shuffled = data.draw(st.permutations(rules))
    assert _run(item, rules, tpl).model_dump_json() == _run(item, shuffled, tpl).model_dump_json()


@PROPERTY
@given(listings(), rule_lists(), templates, products, accounts)
def test_rules_engine_properties_never_raises(
    item: Any, rules: list[Rule], tpl: Any, product: Any, account: Any
) -> None:
    rep = _run(item, rules, tpl, product, account)
    assert rep.status in ("passed", "failed")


@PROPERTY
@given(listings(), rule_lists(), templates)
def test_rules_engine_properties_passed_iff_no_error_item(
    item: Any, rules: list[Rule], tpl: Any
) -> None:
    rep = _run(item, rules, tpl)
    has_error = any(e.severity is Severity.ERROR for e in rep.errors)
    assert (rep.status == "passed") is (not has_error)
    assert rep.passed is (not has_error)
    assert (rep.error_code is None) is (not has_error)


@PROPERTY
@given(listings(), rule_lists(), templates)
def test_rules_engine_properties_rule_ids_known(item: Any, rules: list[Rule], tpl: Any) -> None:
    rep = _run(item, rules, tpl)
    known = {r.id for r in rules if r.enabled}
    for err in rep.errors:
        if err.rule_id is None:
            assert tpl is not None
        else:
            assert err.rule_id in known
        assert err.source.value == "rules_engine"


def _key(err: Any) -> tuple[str, str, Any]:
    return (err.field, err.error_code.value, err.rule_id)


@PROPERTY
@given(listings(), rule_lists(), tenant_rule_lists(), templates, products)
def test_rules_engine_properties_tenant_rule_only_tightens(
    item: Any, rules: list[Rule], tenant_rules: list[Rule], tpl: Any, product: Any
) -> None:
    platform = [r for r in rules if r.tenant_id is None]
    base = _run(item, platform, tpl, product)
    blocking = [e for e in base.errors if e.severity is Severity.ERROR]
    if len(blocking) > 500:
        return  # above the cap the cap decides, not the tenant (test_report_shape_cap_*)
    ids = {r.id for r in platform}
    full = _run(item, platform + [r for r in tenant_rules if r.id not in ids], tpl, product)
    full_keys = {_key(e) for e in full.errors}
    for err in blocking:
        assert _key(err) in full_keys
    if base.status == "failed":
        assert full.status == "failed"


@PROPERTY
@given(listings(), rule_lists(), templates)
def test_rules_engine_properties_hostile_text_never_raises(
    item: Any, rules: list[Rule], tpl: Any
) -> None:
    rep = _run(item, rules, tpl)
    for err in rep.errors:
        assert 0 < len(err.message) <= 1000
        assert err.suggestion is None or len(err.suggestion) <= 1000
