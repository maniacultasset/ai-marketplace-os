"""SI13 T9 (AC8; CP-D11): the question for a child item's variation-axis row asks the child
product; every other row (a style-level axis row, a child's other fact) asks the listing's
product."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from app.services import issue_questions


@dataclass
class Row:
    id: uuid.UUID
    field_path: str
    listing_item_id: uuid.UUID | None = None
    error_code: str = "MISSING_ATTRIBUTE"
    error_class: str = "missing_data"
    source: str = "amazon_adapter"
    severity: str = "error"
    message: str = "m"
    fix_action: str | None = None
    marketplace_code: str | None = "90220"


@dataclass
class Item:
    id: uuid.UUID
    product_id: uuid.UUID
    item_role: str


@dataclass
class Definition:
    key: str
    storage: str = "attributes"
    value_type: str = "text"
    vocabulary: Any = None
    unit: Any = None


class _Uow:
    def __init__(self, items: list[Item]) -> None:
        self.asked: list[tuple[uuid.UUID, str]] = []
        outer = self

        class Items:
            async def for_listing(self, listing_id: uuid.UUID) -> list[Item]:
                return items

        class Defs:
            async def resolve(self, names: Any) -> list[Definition]:
                return [Definition(n) for n in names]

        class Tasks:
            async def open_fact_task(self, product: uuid.UUID, target: str, *rest: Any) -> Any:
                outer.asked.append((product, target))
                return uuid.uuid4()

        class Issues:
            async def link_ids(self, ids: Any, task: Any) -> int:
                return len(ids)

        self.listing_items = Items()
        self.attribute_definitions = Defs()
        self.human_tasks = Tasks()
        self.listing_issues = Issues()


@dataclass
class Listing:
    id: uuid.UUID
    product_id: uuid.UUID


@pytest.mark.parametrize(
    ("path", "on_child", "want_child"),
    [
        ("attributes.size", True, True),
        ("attributes.color", True, True),
        ("attributes.size", False, False),  # a style-level row
        ("attributes.fabric_type", True, False),  # not an axis
    ],
)
async def test_question_owner(path: str, on_child: bool, want_child: bool) -> None:
    style, child = uuid.uuid4(), uuid.uuid4()
    item = Item(uuid.uuid4(), child, "child")
    uow = _Uow([item, Item(uuid.uuid4(), style, "parent")])
    row = Row(uuid.uuid4(), path, item.id if on_child else None)
    await issue_questions.open_questions(
        uow,  # type: ignore[arg-type]
        Listing(uuid.uuid4(), style),
        critical_keys=frozenset(),
        template=None,
        rows=[row],
    )
    ((owner, target),) = uow.asked
    assert owner == (child if want_child else style)
    assert target == "facts." + path
