"""G1 (CANON.md:137): the grouped Inbox and the answer for many products as `app_rw` on `mos_iso`.
A brand's groups hold only its own rows, and another brand's question ids answer exactly as
unknown ones do, with nothing written in the brand that owns them."""

from __future__ import annotations

import types
import uuid
from typing import Any

import psycopg
import pytest
from tests.integration import factories as f
from tests.integration.generation_support import GenWorld, build_gen_world

pytestmark = pytest.mark.usefixtures("iso_settings")
Conn = psycopg.Connection[tuple[object, ...]]
SPEC: dict[str, Any] = {"kind": "enum", "allowed_values": ["black", "red", "maroon"]}
TARGET = "facts.attributes.color"
PATH = "/api/v1/inbox/groups/answer"


@pytest.fixture
async def world(clean_tenants: None, admin_conn: Conn, monkeypatch: pytest.MonkeyPatch) -> GenWorld:
    from app.core.config import get_settings
    from app.services import listing_display

    async def no_recheck(*_: Any) -> None:
        return None

    built = await build_gen_world(admin_conn, monkeypatch)
    monkeypatch.setenv("INBOX_GROUPS_ENABLED", "true")
    monkeypatch.setattr(listing_display, "recheck_queue", no_recheck)
    get_settings.cache_clear()
    return built


async def _questions(world: GenWorld, slug: str, n: int) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """`n` new standalone products of `slug`, each with an open colour question."""
    from app.repositories.human_tasks import HumanTaskRepository
    from app.repositories.transactions import unit_of_work

    tenant = world.tenant(slug)
    out = []
    for _ in range(n):
        product = f.product(world.conn, tenant)
        async with unit_of_work(tenant) as session:
            assert await HumanTaskRepository(session, tenant).open_photo_fact_task(
                product_id=product,
                kind="missing_fact",
                target_path=TARGET,
                question="Add the colour on the product.",
                reason="Amazon needs the colour.",
                expected_answer=SPEC,
                suggestions=[],
                evidence=[],
            )
        task = world.scalar(
            "SELECT id FROM human_tasks WHERE product_id = %s AND status = 'open'", [product]
        )
        out.append((task, product))
    return out


async def test_iso_groups_hold_only_the_callers_rows(world: GenWorld) -> None:
    from app.services.inbox_groups import list_groups

    theirs = await _questions(world, "bharat", 3)
    mine = await _questions(world, "acme", 2)
    for view in ("all", "questions", "issues"):
        body = await list_groups(world.principal("operator@acme.test"), view)
        seen = {m["task_id"] for g in body["groups"] for m in g["members"]}
        seen |= {m["product_id"] for g in body["groups"] for m in g["members"]}
        assert not seen & {x for pair in theirs for x in pair}, view
        if view != "issues":
            assert {t for t, _ in mine} <= seen, view


async def test_iso_another_brands_questions_answer_as_unknown_ones(world: GenWorld) -> None:
    from app.services.inbox_groups import answer_group
    from app.services.inbox_groups_model import task_key

    theirs = await _questions(world, "bharat", 2)
    unknown = uuid.uuid4()
    ids = [t for t, _ in theirs] + [unknown]
    body = types.SimpleNamespace(group=task_key("missing_fact", TARGET, SPEC), task_ids=ids,
                                 value="red")  # fmt: skip
    out = await answer_group(world.principal("operator@acme.test"), str(uuid.uuid4()), PATH, body)
    assert out["applied"] == [] and out["listings"] == []
    assert sorted(out["skipped"], key=lambda s: str(s["task_id"])) == sorted(
        ({"task_id": str(t), "product_id": None, "reason": "gone"} for t in ids),
        key=lambda s: str(s["task_id"]),
    )
    for task, product in theirs:
        assert world.scalar("SELECT status FROM human_tasks WHERE id = %s", [task]) == "open"
        assert world.scalar("SELECT attributes -> 'color' FROM products WHERE id = %s",
                            [product]) is None  # fmt: skip
    audited = world.count("audit_log", "entity_id = ANY(%s)", [[p for _, p in theirs]])
    assert audited == 0
