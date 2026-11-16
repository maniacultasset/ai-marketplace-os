"""G1: the grouped Inbox's pure model (group keys, scope, the never-overwrite rule, the order)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.services import inbox_groups_model as m
from app.services.auto_fix.text import question_for
from app.services.tasks import FACT_TARGET
from mkos_schemas.codes import ErrorCode

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
        ("missing_fact", "facts.marketer", {"kind": "text"}, "brand"),
        ("missing_fact", "facts.packer", {"kind": "text"}, "brand"),
        ("missing_fact", "facts.importer", {"kind": "text"}, "brand"),
        ("missing_fact", "facts.consumer_care", {"kind": "text"}, "per_item"),
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
    assert m.has_value("facts.packer", _row(), _row(packer_party_id=uuid.uuid4()))
    assert not m.has_value("facts.importer", _row(manufacturer_party_id=uuid.uuid4()), None)


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


def _photo_issue(listing: uuid.UUID, product: uuid.UUID, *, at: datetime = T0) -> m.Member:
    key = m.issue_key("IMAGE_NON_COMPLIANT", "listing", "amazon", "error")
    return _member(
        key,
        at=at,
        blocking=True,
        item_kind="issue",
        scope="per_item",
        product_id=product,
        listing_ids=(listing,),
    )


def test_an_issue_group_lists_one_row_per_listing() -> None:
    """Prod 16 Nov (brand Yash): 49 listings each missing the front and the size chart (two
    `IMAGE_NON_COMPLIANT` rows at `listing`, one per view) listed 98 identical rows."""
    members: list[m.Member] = []
    listings = [(uuid.uuid4(), uuid.uuid4()) for _ in range(49)]
    for i, (listing, product) in enumerate(listings):
        members.append(_photo_issue(listing, product, at=T0 + timedelta(minutes=i)))
        members.append(_photo_issue(listing, product, at=T0 + timedelta(minutes=i, seconds=1)))
    (group,) = m.build_groups(members)
    assert (group.count, group.listings, group.products) == (98, 49, 49)
    assert len(group.members) == 49 and not group.members_more
    assert {r.listing_ids[0] for r in group.members} == {lid for lid, _ in listings}
    assert all(r.items == 2 for r in group.members)
    assert sum(r.items for r in group.members) == group.count
    newest = group.members[0]
    assert newest.listing_ids[0] == listings[-1][0]
    assert newest.item_id == newest.item_ids[0]
    assert newest.at == T0 + timedelta(minutes=48, seconds=1)
    every = {i for r in group.members for i in r.item_ids}
    assert every == {x.item_id for x in members}


def test_a_listing_with_one_issue_is_one_row_of_one_item() -> None:
    listing, product = uuid.uuid4(), uuid.uuid4()
    (group,) = m.build_groups([_photo_issue(listing, product)])
    assert (group.count, len(group.members), group.members[0].items) == (1, 1, 1)


def test_questions_stay_one_row_each() -> None:
    product = uuid.uuid4()
    members = [_member("a", product_id=product, listing_ids=(uuid.uuid4(),)) for _ in range(3)]
    (group,) = m.build_groups(members)
    assert (group.count, len(group.members)) == (3, 3)
    assert all(r.items == 1 and r.item_ids == (r.item_id,) for r in group.members)


def test_an_issue_without_a_listing_is_its_own_row() -> None:
    members = [_member("b", item_kind="issue", scope="per_item") for _ in range(2)]
    (group,) = m.build_groups(members)
    assert (group.count, len(group.members)) == (2, 2)


def test_rows_are_capped_but_every_item_counted() -> None:
    members: list[m.Member] = []
    for _ in range(m.MEMBERS_MAX + 2):
        listing, product = uuid.uuid4(), uuid.uuid4()
        members += [_photo_issue(listing, product), _photo_issue(listing, product)]
    (group,) = m.build_groups(members)
    assert group.count == 2 * (m.MEMBERS_MAX + 2)
    assert len(group.members) == m.MEMBERS_MAX and group.members_more


def test_order_blocking_then_size_then_newest() -> None:
    members = [_member("big") for _ in range(5)]
    members += [_member("blocking", blocking=True)]
    members += [_member("old", at=T0 - timedelta(days=1)), _member("new")]
    order = [g.key for g in m.build_groups(members)]
    assert order == ["blocking", "big", "new", "old"]


@pytest.mark.parametrize(
    ("status", "paused", "questions", "sections", "block"),
    [
        ("created", False, 0, True, "not_written"),
        ("created", True, 2, False, "not_written"),
        ("generating", True, 1, True, "busy"),
        ("publishing", False, 0, True, "busy"),
        ("approved", False, 0, False, "nothing_to_redraft"),
        ("generating", False, 0, False, "nothing_to_redraft"),
        ("blocked", True, 1, True, "paused"),
        ("blocked", False, 1, True, "other_questions_open"),
        ("blocked", False, 0, True, None),
        ("approved", False, 0, True, None),
        ("failed", False, 0, True, None),
    ],
)
def test_redraft_block(
    status: str, paused: bool, questions: int, sections: bool, block: str | None
) -> None:
    assert m.redraft_block(status, paused, questions, sections) == block


@pytest.mark.parametrize(
    "code",
    [ErrorCode.FACT_NEEDS_CONFIRMATION, ErrorCode.FACT_MISMATCH, ErrorCode.VALUE_NOT_ALLOWED],
)
def test_a_confirm_or_correct_question_is_its_own_per_item_group(code: ErrorCode) -> None:
    _question, reason = question_for(code, "color", "Colour")
    found = m.task_code(reason)
    assert found == code.value
    plain = m.task_key("missing_fact", "facts.attributes.color", GENDER)
    keyed = m.task_key("missing_fact", "facts.attributes.color", GENDER, found)
    assert keyed == f"{plain}|{code.value}"
    assert m.task_scope("missing_fact", "facts.attributes.color", GENDER, found) == "per_item"


def test_a_missing_value_question_stays_in_the_plain_group() -> None:
    _question, reason = question_for(ErrorCode.MISSING_ATTRIBUTE, "color", "Colour")
    found = m.task_code(reason)
    assert found == "MISSING_ATTRIBUTE"
    assert m.task_key("missing_fact", "facts.attributes.color", GENDER, found) == m.task_key(
        "missing_fact", "facts.attributes.color", GENDER
    )
    assert m.task_scope("missing_fact", "facts.attributes.color", GENDER, found) == "product_fact"
    assert m.task_code("Amazon needs it for this category.") is None
    assert m.task_code(None) is None


def test_the_manufacturer_question_names_the_brand_setting() -> None:
    question, _reason = question_for(ErrorCode.MISSING_ATTRIBUTE, "manufacturer", "manufacturer")
    assert question == (
        "Add the manufacturer. Set it once for the brand in Settings > Compliance, or on this "
        "product."
    )
    confirm, _ = question_for(ErrorCode.FACT_NEEDS_CONFIRMATION, "manufacturer", "manufacturer")
    assert confirm == "Confirm the manufacturer on the product."


async def test_a_fact_that_feeds_no_section_is_nothing_to_redraft() -> None:
    """`redraft` is true only with sections to write: a listing the answer reaches no section of
    is `nothing_to_redraft`, whatever else holds it."""
    import types

    from app.repositories.inbox_groups import ListingRef
    from app.services.inbox_groups import listing_verdicts

    async def counts(*_: object, **__: object) -> dict[uuid.UUID, int]:
        return {}

    listings = types.SimpleNamespace(open_question_counts=counts)
    uow = types.SimpleNamespace(inbox_groups=types.SimpleNamespace(listings=listings))
    ref = ListingRef(uuid.uuid4(), uuid.uuid4(), "amazon", "approved", False)
    (nothing,) = await listing_verdicts(uow, [ref], [])  # type: ignore[arg-type]
    (colour,) = await listing_verdicts(uow, [ref], ["facts.attributes.color"])  # type: ignore[arg-type]
    assert (nothing["redraft"], nothing["redraft_blocked"], nothing["sections"]) == (
        False, "nothing_to_redraft", [],
    )  # fmt: skip
    assert (colour["redraft"], colour["redraft_blocked"], colour["open_questions"]) == (
        True, None, None,
    )  # fmt: skip
