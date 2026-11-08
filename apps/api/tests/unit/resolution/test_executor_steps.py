"""SI13 T7 (AC1, AC2, AC9; CP-D8): the executor's human steps. A `seller/recategorize` or
`seller/edit_identifier` step opens no inbox question and is never turned into staff; any other
seller step on a path the inbox cannot take is still staff (SI11 round 4)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest
from app.services.resolution import run
from app.services.resolution.plan import Step


@dataclass
class Row:
    id: uuid.UUID
    field_path: str
    marketplace_code: str | None = None
    raw_message: str | None = None
    message: str = "m"
    plain_message: str | None = None


class _Issues:
    def __init__(self) -> None:
        self.linked: list[Any] = []

    async def unlinked(self, ids: Any) -> list[uuid.UUID]:
        return list(ids)

    async def link_ids(self, ids: Any, task: Any) -> int:
        self.linked.append((list(ids), task))
        return len(ids)


class _Tasks:
    def __init__(self) -> None:
        self.opened: list[tuple[str, str]] = []

    async def open_account_task(self, kind: str, account: Any, target: str, *rest: Any) -> Any:
        self.opened.append((kind, target))
        return uuid.uuid4()


class _Uow:
    def __init__(self) -> None:
        self.listing_issues = _Issues()
        self.human_tasks = _Tasks()


@dataclass
class Listing:
    id: uuid.UUID
    marketplace: str = "amazon"
    category: str = "kurta"
    marketplace_account_id: uuid.UUID | None = field(default_factory=uuid.uuid4)


@pytest.fixture
def asked(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    calls: list[Any] = []

    async def open_questions(uow: Any, listing: Any, **kw: Any) -> list[uuid.UUID]:
        calls.append(kw["rows"])
        return []

    async def images(*_: Any) -> tuple[()]:
        return ()

    import app.services.issue_questions as questions

    monkeypatch.setattr(questions, "open_questions", open_questions)
    monkeypatch.setattr(run, "_images", images)
    return calls


def _uow() -> Any:
    uow: Any = _Uow()

    class _Listings:
        async def preview_source(self, *_: Any) -> None:
            return None

    class _Facts:
        async def critical_keys(self) -> frozenset[str]:
            return frozenset()

    uow.listings = _Listings()
    uow.product_facts = _Facts()
    return uow


@pytest.mark.parametrize(
    ("path", "action"),
    [
        ("facts.category", "recategorize"),
        ("facts.identifier", "edit_identifier"),
        ("variants[A-1].facts.identifier", "edit_identifier"),
    ],
)
async def test_a_seller_action_opens_no_question_and_stays_seller(
    asked: list[Any], path: str, action: str
) -> None:
    row = Row(uuid.uuid4(), path)
    uow = _uow()
    step = Step(row.id, "seller", "mapping", action)
    opened = await run._human_steps(uow, Listing(uuid.uuid4()), [step], {row.id: row})
    assert asked == [] and opened == {}
    assert uow.human_tasks.opened == []


async def test_a_seller_ask_on_the_category_still_goes_to_staff(asked: list[Any]) -> None:
    row = Row(uuid.uuid4(), "facts.category")
    uow = _uow()
    step = Step(row.id, "seller", "seller_fact", "ask")
    opened = await run._human_steps(uow, Listing(uuid.uuid4()), [step], {row.id: row})
    assert asked == []  # turned into staff before the inbox is asked (round 4)
    assert list(opened) == ["staff"]
    assert uow.human_tasks.opened[0][0] == "marketplace_error_review"


def test_the_held_burst_has_no_step_while_the_category_step_exists() -> None:
    from app.services.resolution.plan import IssueView, PlanContext, plan

    def view(path: str, code: str, klass: str) -> IssueView:
        return IssueView(uuid.uuid4(), "amazon_adapter", path, code, klass, "error")

    cat = view("facts.category", "CATEGORY_MISMATCH", "category_mismatch")
    burst = [view(f"attributes.a{n}", "MISSING_ATTRIBUTE", "missing_data") for n in range(3)]
    ctx = PlanContext(recategorizable=True)
    assert [s.action for s in plan([cat, *burst], ctx)] == ["recategorize"]
    # once the category item closes, SI11's normal steps come back
    assert len(plan(burst, ctx)) == 3
