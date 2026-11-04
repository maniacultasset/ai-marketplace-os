"""DW2 tester round 1 (AC8, AC10, AC11): property checks on the PTD schema check. For any
submitted attribute values, `check_item` never raises, never puts the submitted value in a
message or `value_submitted`, sorts its output and keeps to the error cap."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from app.adapters.amazon import constants as c
from app.adapters.amazon import schema_check
from app.adapters.amazon.definitions import canonical_digest, parse_schema
from app.adapters.base import ProductTypeSchema
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from mkos_schemas.codes import ErrorCode
from tests.support.fake_marketplace import ptd_fixture

MARKER = "QZXV"


def _prepared(schema: dict[str, Any], *, enforced: bool = True) -> ProductTypeSchema:
    ref = ProductTypeSchema(
        definition_id=uuid.uuid4(),
        product_type="KURTA",
        product_type_version="UFAKE0001",
        requirements="LISTING_PRODUCT_ONLY",
        enforced=enforced,
        digest=canonical_digest(schema),
    )

    async def load(_id: uuid.UUID) -> dict[str, Any]:
        return schema

    return asyncio.run(schema_check.prepare(ref, load))


KURTA = _prepared(json.loads(ptd_fixture("KURTA.schema.json")))
NAMES = sorted(json.loads(ptd_fixture("KURTA.schema.json"))["properties"])

leaf = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**40), max_value=2**40),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=40).map(lambda s: MARKER + s),
)
value = st.recursive(
    leaf,
    lambda inner: st.one_of(
        st.lists(inner, max_size=4),
        st.dictionaries(st.sampled_from(["value", "language_tag", "marketplace_id", "x"]), inner),
    ),
    max_leaves=12,
)
attributes = st.dictionaries(st.sampled_from([*NAMES, "zz_unknown"]), value, max_size=12)


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(attrs=attributes, enforced=st.booleans())
def test_check_item_never_raises_or_leaks(attrs: dict[str, Any], enforced: bool) -> None:
    schema = (
        KURTA
        if enforced
        else _prepared(json.loads(ptd_fixture("KURTA.schema.json")), enforced=False)
    )
    index = {
        "item_name": ("content.title",),
        "generic_keyword": ("keywords.search_terms", "keywords.hidden_terms"),
    }
    items = schema_check.check_item(attrs, schema, index)
    assert len(items) <= c.MAX_SCHEMA_ERRORS + 1
    for item in items:
        assert item.value_submitted is None
        assert MARKER not in item.message
        assert MARKER not in (item.raw_code or "")
        assert item.marketplace is not None
    keys = [(i.field, i.error_code.value) for i in items[: c.MAX_SCHEMA_ERRORS]]
    assert keys == sorted(keys)


def test_many_missing_required_capped_with_one_summary() -> None:
    required = [f"attr_{n:03d}" for n in range(c.MAX_SCHEMA_ERRORS + 20)]
    schema = _prepared({"type": "object", "required": required})
    items = schema_check.check_item({}, schema, {})
    assert len(items) == c.MAX_SCHEMA_ERRORS + 1
    assert items[-1].error_code is ErrorCode.SCHEMA_INVALID
    assert "20 more" in items[-1].message


@given(depth=st.integers(min_value=1, max_value=120))
@settings(max_examples=30, deadline=None)
def test_moderately_nested_schema_compiles_or_fails_closed(depth: int) -> None:
    body = ('{"properties":{"a":' * depth) + "{}" + ("}}" * depth)
    schema = parse_schema(body.encode())
    schema_check.clear_cache()
    prepared = _prepared(schema)
    out = schema_check.check_item({"a": {}}, prepared, {})
    assert all(i.error_code is ErrorCode.SCHEMA_INVALID for i in out)
