"""G1: the grouped Inbox's pure model (group keys, scope, the never-overwrite rule, the order)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.services import inbox_groups_model as m
from app.services.tasks import FACT_TARGET

T0 = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)
GENDER = {"kind": "enum", "allowed_values": ["women", "men", "unisex"]}


@pytest.mark.parametrize(
    ("path", "family"),
    [
        ("images[3]", "images[]"),
        ("content.bullets[2]", "content.bullets[]"),
        ("variants[KTEST-001-M].facts.color", "variants[].facts.color"),
        ("facts.attributes.gender", "facts.attributes.gender"),
        (None, ""),
    ],
)
def test_field_family(path: str | None, family: str) -> None:
    assert m.field_family(path) == family


def test_task_key_is_stable_and_ignores_value_order() -> None:
    a = m.task_key("missing_fact", "facts.gender", GENDER)
    b = m.task_key(
        "missing_fact",
        "facts.gender",
        {"allowed_values": ["unisex", "men", "women"], "kind": "enum"},
    )
    assert a == b
    assert a.startswith("t|missing_fact|facts.gender|") and len(a.rsplit("|", 1)[1]) == 12


def test_another_allowed_list_is_another_group() -> None:
    narrow = {"kind": "enum", "allowed_values": ["women", "men"]}
    assert m.task_key("missing_fact", "facts.gender", GENDER) != m.task_key(
        "missing_fact", "facts.gender", narrow
    )
    assert m.task_key("missing_fact", "facts.gender", GENDER) != m.task_key(
        "conflicting_facts", "facts.gender", GENDER
    )


def test_issue_key_drops_the_index_and_keeps_marketplace_and_severity() -> None:
    a = m.issue_key("IMAGE_NON_COMPLIANT", "images[0]", "amazon", "error")
    assert a == m.issue_key("IMAGE_NON_COMPLIANT", "images[4]", "amazon", "error")
    assert a != m.issue_key("IMAGE_NON_COMPLIANT", "images[0]", "flipkart", "error")
    assert a != m.issue_key("IMAGE_NON_COMPLIANT", "images[0]", "amazon", "warning")


def test_a_long_key_is_hashed_within_bounds() -> None:
    key = m.issue_key("FIELD_TOO_LONG", "content." + "x" * 300, "amazon", "error")
    assert key.startswith("h|") and len(key) <= m.KEY_MAX
    assert key == m.issue_key("FIELD_TOO_LONG", "content." + "x" * 300, "amazon", "error")


@pytest.mark.parametrize(
    ("kind", "target", "spec", "scope"),
    [
        ("missing_fact", "facts.manufacturer", {"kind": "text"}, "brand"),
        ("missing_fact", "facts.identifier", {"kind": "text"}, "exemption"),
        ("missing_fact", "facts.gender", GENDER, "product_fact"),
        ("missing_fact", "facts.hsn_code", {"kind": "text"}, "product_fact"),
        ("missing_fact", "facts.country_of_origin", GENDER, "product_fact"),
        ("missing_fact", "facts.attributes.fabric_type", {"kind": "text"}, "product_fact"),
        ("missing_fact", "facts.attributes.fiber_composition",
         {"kind": "percent_composition"}, "product_fact"),
        ("missing_fact", "facts.attributes.number_of_pieces", {"kind": "number"}, "product_fact"),
        # never overwritten: each product already holds a value the photos disagree with
        ("conflicting_facts", "facts.attributes.color", GENDER, "per_item"),
        ("missing_fact", "facts.attributes.color", {"kind": "multi_enum"}, "per_item"),
        ("missing_fact", "facts.marketer", {"kind": "text"}, "per_item"),
        ("image_replacement", None, {"kind": "file"}, "per_item"),
        ("reconnect_account", "account.oauth", {"kind": "reconnect"}, "per_item"),
    ],
)  # fmt: skip
def test_scope(kind: str, target: str | None, spec: dict[str, Any], scope: str) -> None:
    assert m.task_scope(kind, target, spec) == scope


def test_product_fact_targets_are_the_ones_tasks_answer() -> None:
    for target in (
        "facts.gender",
        "facts.brand_name",
        "facts.hsn_code",
        "facts.country_of_origin",
        "facts.attributes.neckline",
        "facts.manufacturer",
        "facts.identifier",
        "facts.attributes.Bad",
    ):
        assert bool(m.PRODUCT_FACT_TARGET.match(target)) == bool(FACT_TARGET.match(target))


def test_task_field() -> None:
    assert m.task_field("facts.attributes.gender") == "gender"
    assert m.task_field("facts.manufacturer") == "manufacturer"
    assert m.task_field("account.oauth") == "account.oauth"
    assert m.task_field(None) is None


def _row(**columns: Any) -> m.FactRow:
    attributes = columns.pop("attributes", {})
    return m.FactRow(
        id=uuid.uuid4(),
        parent_id=columns.pop("parent_id", None),
        is_style=columns.pop("is_style", False),
        attributes=attributes,
        columns=columns,
    )


def test_has_value_attribute_own_or_style() -> None:
    filled = {"fabric_type": {"value": "cotton", "source": "seller"}}
    assert m.has_value("facts.attributes.fabric_type", _row(attributes=filled), None)
    assert m.has_value("facts.attributes.fabric_type", _row(), _row(attributes=filled))
    assert not m.has_value("facts.attributes.fabric_type", _row(), _row())
    empty = {"fabric_type": {"value": None}}
    assert not m.has_value("facts.attributes.fabric_type", _row(attributes=empty), None)


def test_has_value_columns() -> None:
    assert m.has_value("facts.gender", _row(gender="women"), None)
    assert not m.has_value("facts.gender", _row(gender=None), None)
    assert m.has_value("facts.hsn_code", _row(), _row(hsn_code="6204"))
    assert m.has_value("facts.manufacturer", _row(manufacturer_party_id=uuid.uuid4()), None)
    assert not m.has_value("facts.manufacturer", _row(), None)


def test_has_value_identifier() -> None:
    assert m.has_value("facts.identifier", _row(identifier_status="provided"), None)
    assert m.has_value("facts.identifier", _row(identifier_status="exempt"), None)
    assert not m.has_value("facts.identifier", _row(identifier_status=None), None)
    style = m.FactRow(uuid.uuid4(), None, True, {}, {}, variants_without_status=2)
    assert not m.has_value("facts.identifier", style, None)
    done = m.FactRow(uuid.uuid4(), None, True, {}, {}, variants_without_status=0)
    assert m.has_value("facts.identifier", done, None)


def _member(key: str, *, at: datetime = T0, blocking: bool = False, **fields: Any) -> m.Member:
    return m.Member(
        key=key,
        item_kind=fields.pop("item_kind", "task"),
        scope=fields.pop("scope", "product_fact"),
        at=at,
        blocking=blocking,
        item_id=uuid.uuid4(),
        **fields,
    )


def test_build_groups_counts_every_member_once() -> None:
    product = uuid.uuid4()
    listing = uuid.uuid4()
    members = [
        _member("a", at=T0 + timedelta(minutes=i), product_id=uuid.uuid4(), category="kurta")
        for i in range(34)
    ]
    members += [
        _member(
            "b", item_kind="issue", scope="per_item", product_id=product, listing_ids=(listing,)
        )
    ]
    members += [_member("b", item_kind="issue", scope="per_item", product_id=product)]
    groups = m.build_groups(members)
    assert sum(g.count for g in groups) == len(members)
    first = groups[0]
    assert (first.key, first.count, first.products, first.categories) == ("a", 34, 34, ["kurta"])
    assert first.newest.at == T0 + timedelta(minutes=33)
    second = groups[1]
    assert (second.count, second.products, second.listings, second.answer) == (2, 1, 1, None)


def test_members_are_capped_but_counted() -> None:
    groups = m.build_groups([_member("a") for _ in range(m.MEMBERS_MAX + 5)])
    assert groups[0].count == m.MEMBERS_MAX + 5
    assert len(groups[0].members) == m.MEMBERS_MAX and groups[0].members_more


def test_order_blocking_then_size_then_newest() -> None:
    members = [_member("big") for _ in range(5)]
    members += [_member("blocking", blocking=True)]
    members += [_member("old", at=T0 - timedelta(days=1)), _member("new")]
    order = [g.key for g in m.build_groups(members)]
    assert order == ["blocking", "big", "new", "old"]


@pytest.mark.parametrize(
    ("status", "paused", "redraft"),
    [
        ("created", False, False),
        ("generating", False, False),
        ("blocked", True, False),
        ("blocked", False, True),
        ("approved", False, True),
        ("failed", False, True),
    ],
)
def test_redraft(status: str, paused: bool, redraft: bool) -> None:
    assert m.redraft(status, paused) is redraft
