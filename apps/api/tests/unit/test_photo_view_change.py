"""Client feedback item 7, bullet 3: `photo_view_change.change_view` over a fake unit of work (the
route's case matrix and the real constraints run in tests/integration/test_image_view_api.py)."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from app.core.errors import InputError, LimitError, NotFoundError
from app.services import photo_view_change as pvc
from mkos_schemas.enums import ImageSlot

TENANT = uuid.uuid4()
USER = uuid.uuid4()
PRODUCT = uuid.uuid4()


@dataclass
class Row:
    slot: str
    role: str | None = None
    colourway_id: uuid.UUID | None = None
    position: int = 0
    compliance_status: str = "passed"
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    product_id: uuid.UUID = PRODUCT
    match: bool = True


@dataclass
class World:
    rows: dict[uuid.UUID, Row] = field(default_factory=dict)
    jobs: list[uuid.UUID] = field(default_factory=list)
    audits: list[dict[str, Any]] = field(default_factory=list)
    retired: list[uuid.UUID] = field(default_factory=list)
    reads: list[uuid.UUID] = field(default_factory=list)
    live_jobs: int = 0
    variant: bool = False
    writes: list[tuple[uuid.UUID, str, str | None]] = field(default_factory=list)

    def add(self, *rows: Row) -> None:
        for row in rows:
            self.rows[row.id] = row

    def _unique_ok(self) -> None:
        live = list(self.rows.values())
        mains = [(r.product_id, r.colourway_id) for r in live if r.slot == "main"]
        roles = [(r.product_id, r.colourway_id, r.role) for r in live if r.role is not None]
        assert len(mains) == len(set(mains)), "images_one_main"
        assert len(roles) == len(set(roles)), "images_one_role"
        assert all(r.role is None or r.slot == "secondary" for r in live), "ck_images_role"


class FakeImages:
    def __init__(self, world: World) -> None:
        self.w = world

    async def get_live(self, image_id: uuid.UUID) -> Any:
        row = self.w.rows.get(image_id)
        return None if row is None else SimpleNamespace(**row.__dict__)

    async def lock_tenant_image_jobs(self) -> None: ...

    async def lock_product_images(self, product_id: uuid.UUID) -> None: ...

    async def count_live_image_jobs(self) -> int:
        return self.w.live_jobs

    async def view_holder(self, product_id, colourway_id, slot, role, *, other_than):  # type: ignore[no-untyped-def]
        for r in self.w.rows.values():
            if r.id == other_than or (r.product_id, r.colourway_id) != (product_id, colourway_id):
                continue
            if slot is ImageSlot.MAIN and r.slot == "main":
                return SimpleNamespace(**r.__dict__)
            if slot is ImageSlot.SECONDARY and role is not None and r.role == role:
                return SimpleNamespace(**r.__dict__)
        return None

    async def next_position(self, product_id, colourway_id, slot) -> int:  # type: ignore[no-untyped-def]
        taken = [
            r.position
            for r in self.w.rows.values()
            if (r.product_id, r.colourway_id, r.slot) == (product_id, colourway_id, slot.value)
        ]
        return max(taken, default=-1) + 1

    async def set_view(self, image_id, *, slot, role, position, recheck=False, drop_match=False):  # type: ignore[no-untyped-def]
        row = self.w.rows[image_id]
        row.slot, row.role, row.position = slot.value, role, position
        if recheck:
            row.compliance_status = "pending"
        if drop_match:
            row.match = False
        self.w.writes.append((image_id, slot.value, role))
        self.w._unique_ok()  # every statement keeps the unique indexes
        return True


class FakeUow:
    def __init__(self, world: World) -> None:
        self.images = FakeImages(world)
        w = world

        async def get_by_id(_pid: uuid.UUID) -> Any:
            return SimpleNamespace(parent_product_id=uuid.uuid4() if w.variant else None)

        async def fail_for_source(source: uuid.UUID) -> list[str]:
            w.retired.append(source)
            return []

        async def enqueue(spec: Any) -> Any:
            w.jobs.append(spec.entity_id)
            return SimpleNamespace(job=SimpleNamespace(id=uuid.uuid4()), created=True)

        async def write(**kw: Any) -> None:
            w.audits.append(kw)

        self.products = SimpleNamespace(get_by_id=get_by_id)
        self.image_candidates = SimpleNamespace(fail_for_source=fail_for_source)
        self.job_runs = SimpleNamespace(enqueue=enqueue)
        self.audit = SimpleNamespace(write=write)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> World:
    w = World()

    @asynccontextmanager
    async def fake_uow(*_a: Any, **_k: Any) -> AsyncIterator[FakeUow]:
        yield FakeUow(w)

    async def read(_uow: Any, image_id: uuid.UUID) -> None:
        w.reads.append(image_id)

    async def no_objects(_keys: list[str]) -> None: ...

    monkeypatch.setattr(pvc, "tenant_uow", fake_uow)
    monkeypatch.setattr(pvc, "queue_photo_read", read)
    monkeypatch.setattr(pvc, "delete_candidate_objects", no_objects)
    return w


PRINCIPAL: Any = SimpleNamespace(tenant_id=TENANT, user_id=USER)


def _reasons(w: World) -> list[str]:
    return [a["reason"] for a in w.audits]


def test_view_names() -> None:
    assert pvc.view_name("main", None) == "front"
    assert pvc.view_name("secondary", "back") == "back"
    assert pvc.view_name("secondary", None) == "extra"
    assert pvc.view_name("packaging_label", None) == "packaging_label"


async def test_extra_marked_back_keeps_checks_and_asks_the_read(world: World) -> None:
    extra = Row("secondary")
    world.add(extra)
    change = await pvc.change_view(PRINCIPAL, extra.id, ImageSlot.SECONDARY, "back")
    assert (change.image.slot, change.image.role, change.image.job_id) == (
        ImageSlot.SECONDARY,
        "back",
        None,
    )
    assert change.image.compliance_status == "passed" and change.swapped is None
    assert world.jobs == [] and world.reads == [extra.id]
    assert _reasons(world) == ["extra->back"]
    assert json.loads(world.audits[0]["after"]) == {"slot": "secondary"}


async def test_back_to_front_swaps_and_rechecks_both(world: World) -> None:
    front, back = Row("main"), Row("secondary", "back", position=3)
    world.add(front, back)
    change = await pvc.change_view(PRINCIPAL, back.id, ImageSlot.MAIN, None)
    assert (back.slot, back.role, back.position) == ("main", None, 0)
    assert (front.slot, front.role) == ("secondary", "back")
    assert change.swapped is not None and change.swapped.image_id == front.id
    assert change.image.job_id is not None and change.swapped.job_id is not None
    assert sorted(world.jobs) == sorted([front.id, back.id])
    assert front.compliance_status == back.compliance_status == "pending"
    assert world.retired == [front.id]  # the old Front's candidates
    assert sorted(_reasons(world)) == ["back->front", "front->back"]
    assert world.reads == []  # the re-check job queues the read


async def test_back_and_close_up_swap_without_a_recheck(world: World) -> None:
    back, close = Row("secondary", "back"), Row("secondary", "close_up")
    world.add(back, close)
    change = await pvc.change_view(PRINCIPAL, close.id, ImageSlot.SECONDARY, "back")
    assert (close.role, back.role) == ("back", "close_up")
    assert change.swapped is not None and change.swapped.role == "close_up"
    assert world.jobs == [] and world.retired == []


async def test_front_to_size_chart_swaps_nothing_and_drops_the_match(world: World) -> None:
    front = Row("main")
    world.add(front, Row("size_chart", position=0))
    change = await pvc.change_view(PRINCIPAL, front.id, ImageSlot.SIZE_CHART, None)
    assert change.swapped is None
    assert (front.slot, front.position, front.match) == ("size_chart", 1, False)
    assert world.jobs == [front.id] and world.retired == [front.id]


async def test_other_colour_front_is_not_swapped(world: World) -> None:
    red = uuid.uuid4()
    shared, red_extra = Row("main"), Row("secondary", colourway_id=red)
    world.add(shared, red_extra)
    change = await pvc.change_view(PRINCIPAL, red_extra.id, ImageSlot.MAIN, None)
    assert change.swapped is None and shared.slot == "main" and red_extra.slot == "main"


async def test_same_view_writes_nothing(world: World) -> None:
    back = Row("secondary", "back")
    world.add(back)
    change = await pvc.change_view(PRINCIPAL, back.id, ImageSlot.SECONDARY, "back")
    assert change.image.job_id is None and world.writes == [] and world.audits == []


async def test_refusals(world: World) -> None:
    photo = Row("secondary")
    world.add(photo)
    with pytest.raises(InputError):
        await pvc.change_view(PRINCIPAL, photo.id, ImageSlot.MAIN, "back")
    with pytest.raises(NotFoundError):
        await pvc.change_view(PRINCIPAL, uuid.uuid4(), ImageSlot.MAIN, None)
    world.variant = True
    with pytest.raises(InputError):
        await pvc.change_view(PRINCIPAL, photo.id, ImageSlot.SIZE_CHART, None)
    world.variant = False
    world.live_jobs = 20  # the live image-job cap counts the re-check
    with pytest.raises(LimitError):
        await pvc.change_view(PRINCIPAL, photo.id, ImageSlot.MAIN, None)
    assert world.writes == [] and world.audits == []
