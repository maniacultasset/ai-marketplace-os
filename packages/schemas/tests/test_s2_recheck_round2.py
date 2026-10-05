"""Round 2 re-check: property tests over the round 1 fixes (AC 17 walk, cost_usd, rule guards)."""

from __future__ import annotations

import json
import string
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st
from mkos_schemas.agent import AgentRunRecord
from mkos_schemas.base import SEAM_SECRET_DENY_NAMES, find_seam_secret_key
from mkos_schemas.jobs import GenerationContext, ModelCredentials
from mkos_schemas.rules import Rule
from pydantic import ValidationError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SECRET = "sk-LIVE-ROUND2-SECRET-VALUE"


def _fixture(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text())
    return data


def _spell(name: str, data: st.DataObject) -> str:
    """One of the spellings a producer really uses: snake, SCREAMING, kebab, camel, prefixed."""
    parts = name.split("_")
    style = data.draw(st.sampled_from(["snake", "upper", "kebab", "camel", "pascal", "prefixed"]))
    if style == "upper":
        return name.upper()
    if style == "kebab":
        return "-".join(p.capitalize() for p in parts)
    if style == "camel":
        return parts[0] + "".join(p.capitalize() for p in parts[1:])
    if style == "pascal":
        return "".join(p.capitalize() for p in parts)
    if style == "prefixed":
        return "lwa_" + name
    return name


def _bury(key: str, data: st.DataObject) -> object:
    """Put ``{key: SECRET}`` at a random depth inside dicts, lists and JSON text."""
    node: object = {key: SECRET}
    for _ in range(data.draw(st.integers(min_value=0, max_value=6))):
        wrap = data.draw(st.sampled_from(["dict", "list", "json"]))
        if wrap == "dict":
            node = {"k": node}
        elif wrap == "list":
            node = ["x", node]
        elif not isinstance(node, str):
            # JSON text of JSON text (a top-level JSON string literal) is a known gap, kept in
            # docs/plans/s2/repro/test_s2_round2_repro.py, so it is not drawn here.
            node = json.dumps(node)
    return node


@settings(max_examples=300, deadline=None)
@given(name=st.sampled_from(sorted(SEAM_SECRET_DENY_NAMES)), data=st.data())
def test_walk_finds_deny_name_in_any_spelling_at_any_depth(name: str, data: st.DataObject) -> None:
    key = _spell(name, data)
    assert find_seam_secret_key(_bury(key, data)) is not None


@settings(max_examples=60, deadline=None)
@given(name=st.sampled_from(sorted(SEAM_SECRET_DENY_NAMES)), data=st.data())
def test_generation_context_rejects_buried_secret_without_echoing_it(
    name: str, data: st.DataObject
) -> None:
    ctx = _fixture("GenerationContext")
    field = data.draw(st.sampled_from(["product", "brand_profile"]))
    ctx[field] = {**ctx[field], "extra": _bury(_spell(name, data), data)}
    with pytest.raises(ValidationError) as info:
        GenerationContext.model_validate(ctx)
    assert SECRET not in str(info.value)
    assert SECRET not in info.value.json()


def test_model_credentials_drops_unknown_keys_and_never_echoes_key() -> None:
    creds = ModelCredentials.model_validate(
        {"provider": "openai", "api_key": SECRET, "model": "m", "api_version": "2026-01"}
    )
    assert "api_version" not in creds.model_dump()
    assert SECRET not in repr(creds) and SECRET not in creds.model_dump_json()
    with pytest.raises(ValidationError) as info:
        ModelCredentials.model_validate(
            {"provider": "openai", "api_key": SECRET, "model": "m", "api_base": "http://x"}
        )
    assert SECRET not in str(info.value)


@settings(max_examples=200, deadline=None)
@given(
    st.decimals(
        min_value=Decimal("0"), max_value=Decimal("999999.999999"), places=6, allow_nan=False
    )
)
def test_cost_usd_wire_form_is_six_places_and_round_trips(value: Decimal) -> None:
    record = AgentRunRecord.model_validate({**_fixture("AgentRunRecord"), "cost_usd": value})
    wire = json.loads(record.model_dump_json())["cost_usd"]
    whole, _, frac = wire.partition(".")
    assert len(frac) == 6 and whole.isdigit()
    assert Decimal(wire) == value
    assert AgentRunRecord.model_validate_json(record.model_dump_json()) == record


@settings(max_examples=200, deadline=None)
@given(
    placeholder=st.sampled_from(["value", "limit", "term"]),
    spec=st.text(alphabet=string.ascii_letters + string.digits + "<>^=+-.,%!:", min_size=1),
)
def test_rule_message_refuses_any_spec_or_conversion(placeholder: str, spec: str) -> None:
    assume(spec != ":")  # "{value:}" is an empty spec, which str.format treats as plain {value}
    sep = "" if spec[0] in "!:" else ":"
    message = "{" + placeholder + sep + spec + "}"
    rule = {
        "id": str(uuid4()),
        "marketplace": "amazon",
        "field": "content.title",
        "rule_def": {"type": "max_length", "limit": 5, "message": message},
    }
    with pytest.raises(ValidationError):
        Rule.model_validate(rule)


@pytest.mark.parametrize(
    "rule_def",
    [
        {"type": "pattern", "name": "p", "regex": "a", "must": "match", "message": "m"},
        {"type": "banned_terms", "terms": ["a"], "match": "regex", "message": "m"},
        {"type": "max_length", "limit": 5, "message": "m"},
        {"type": "required_attribute", "attribute": "color", "message": "m"},
    ],
)
def test_tenant_rule_refuses_platform_only_types(rule_def: dict[str, Any]) -> None:
    rule = {
        "id": str(uuid4()),
        "tenant_id": str(uuid4()),
        "marketplace": "amazon",
        "field": "content.title",
        "rule_def": rule_def,
    }
    with pytest.raises(ValidationError):
        Rule.model_validate(rule)
