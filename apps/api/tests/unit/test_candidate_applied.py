"""ADR-0030 amendment (2026-11-11): an accepted candidate answers a create as "already applied"
only while its image is live and still the main of the asking product and colourway. Pure: the
unit of work is a stand-in (the database paths are in `test_image_candidates_api.py`)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from app.core.errors import NotFoundError
from app.services import image_candidates as svc

PRODUCT, RED, BLUE = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def _image(**over: Any) -> Any:
    base = {
        "id": uuid.uuid4(),
        "product_id": PRODUCT,
        "colourway_id": RED,
        "slot": "main",
        "clean_content_hash": "a" * 64,
    }
    return SimpleNamespace(**{**base, **over})


def _candidate(made: Any, **over: Any) -> Any:
    base = {
        "id": uuid.uuid4(),
        "product_id": PRODUCT,
        "status": "accepted",
        "kind": "white_background",
        "accepted_image_id": None if made is None else made.id,
        "job_id": uuid.uuid4(),
    }
    return SimpleNamespace(**{**base, **over})


class _Uow:
    def __init__(self, images: list[Any], made_from: Any = None) -> None:
        live = {i.id: i for i in images}

        async def get_live(image_id: uuid.UUID) -> SimpleNamespace | None:
            return live.get(image_id)

        async def get_by_id(product_id: uuid.UUID) -> object | None:
            return object() if product_id == PRODUCT else None

        async def latest_accepted_from(source_id: uuid.UUID, kind: str) -> Any:
            return made_from

        self.images = SimpleNamespace(get_live=get_live)
        self.products = SimpleNamespace(get_by_id=get_by_id)
        self.image_candidates = SimpleNamespace(latest_accepted_from=latest_accepted_from)


async def test_applied_while_its_image_is_the_live_main_of_the_colourway() -> None:
    made = _image()
    row = _candidate(made)
    uow: Any = _Uow([made])
    assert await svc._still_applied(uow, row, RED) is row


@pytest.mark.parametrize(
    ("images", "colourway"),
    [
        ([], RED),  # the applied image was deleted
        ([_image(slot="secondary")], RED),  # another main replaced it
        ([_image()], BLUE),  # the same photo, asked for another colourway
        ([_image(product_id=uuid.uuid4())], RED),  # never another product's image
    ],
)
async def test_not_applied_once_the_image_is_gone_replaced_or_elsewhere(
    images: list[Any], colourway: uuid.UUID
) -> None:
    made = images[0] if images else _image()
    row = _candidate(made)
    uow: Any = _Uow(images)
    assert await svc._still_applied(uow, row, colourway) is None


@pytest.mark.parametrize("status", ["queued", "ready", "rejected", "failed"])
async def test_only_an_accepted_row_can_be_applied(status: str) -> None:
    made = _image()
    row = _candidate(made, status=status)
    uow: Any = _Uow([made])
    assert await svc._still_applied(uow, row, RED) is None


def test_the_answer_names_the_applied_image_and_no_job() -> None:
    made = _image()
    row = _candidate(made)
    answer = svc._accepted(row, with_job=False)
    assert answer == {
        "candidate_id": str(row.id),
        "status": "accepted",
        "job_id": None,
        "poll_url": None,
        "accepted_image_id": str(made.id),
    }
    queued = _candidate(None, status="queued")
    assert svc._accepted(queued, with_job=True)["accepted_image_id"] is None


async def test_a_demoted_source_answers_its_applied_candidate() -> None:
    """The page was read before the accept: its old main asks again."""
    old = _image(slot="secondary")
    made = _image()
    row = _candidate(made)
    uow: Any = _Uow([old, made], made_from=row)
    body = svc.CandidateRequest(kind="white_background", source_image_id=old.id)
    assert await svc._resolve_key(uow, PRODUCT, body) is row


@pytest.mark.parametrize("applied", [False, True])
async def test_a_demoted_source_with_nothing_applied_is_404(applied: bool) -> None:
    old = _image(slot="secondary")
    made = _image(slot="secondary")  # replaced since
    uow: Any = _Uow([old, made], made_from=_candidate(made) if applied else None)
    body = svc.CandidateRequest(kind="fix", source_image_id=old.id)
    with pytest.raises(NotFoundError):
        await svc._resolve_key(uow, PRODUCT, body)


@pytest.mark.parametrize(("slot", "applied"), [("size_chart", True), ("main", False)])
async def test_a_size_chart_is_applied_while_its_image_is_the_chart(
    slot: str, applied: bool
) -> None:
    made = _image(slot=slot, colourway_id=None)
    row = _candidate(made, kind="size_chart")
    uow: Any = _Uow([made])
    assert (await svc._still_applied(uow, row, None) is row) is applied


class _Store:
    def __init__(self) -> None:
        self.signed: list[tuple[str, int]] = []

    async def presign_get(self, bucket: str, key: str, *, expires_s: int) -> str:
        self.signed.append((key, expires_s))
        return f"https://store.test/{key}?X-Amz-Signature=x"


def _signing(monkeypatch: pytest.MonkeyPatch, uow: Any) -> _Store:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_uow(tenant_id: uuid.UUID, *, user_id: uuid.UUID | None = None) -> Any:
        yield uow

    store = _Store()
    monkeypatch.setattr(svc, "tenant_uow", fake_uow)
    monkeypatch.setattr(svc, "get_storage", lambda: store)
    monkeypatch.setattr(svc, "images_bucket", lambda: "images")
    return store


def _with_row(uow: Any, row: Any) -> Any:
    async def get(candidate_id: uuid.UUID) -> Any:
        return row if candidate_id == row.id else None

    uow.image_candidates.get = get
    return uow


_PRINCIPAL: Any = SimpleNamespace(tenant_id=uuid.uuid4(), user_id=uuid.uuid4())


async def test_an_applied_answer_signs_the_candidates_own_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale page shows the processed photo at once: the candidate's stored image, for 10
    minutes."""
    made = _image()
    row = _candidate(made, colourway_id=RED, storage_key="t/candidates/c.jpg")
    store = _signing(monkeypatch, _with_row(_Uow([made]), row))
    answer = svc._accepted(row, with_job=False)
    url = await svc._applied_preview(_PRINCIPAL, answer)
    assert url is not None and "X-Amz-Signature" in url
    assert store.signed == [("t/candidates/c.jpg", svc.PREVIEW_TTL_S)]


async def test_no_signed_image_once_it_is_no_longer_the_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replayed "already applied" answer signs nothing after its image was replaced."""
    made = _image(slot="secondary")
    row = _candidate(made, colourway_id=RED, storage_key="t/candidates/c.jpg")
    store = _signing(monkeypatch, _with_row(_Uow([made]), row))
    answer = {**svc._accepted(row, with_job=False), "accepted_image_id": str(made.id)}
    assert await svc._applied_preview(_PRINCIPAL, answer) is None
    assert store.signed == []


async def test_no_signed_image_for_any_other_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _candidate(None, status="queued", colourway_id=RED, storage_key=None)
    store = _signing(monkeypatch, _with_row(_Uow([]), row))
    assert await svc._applied_preview(_PRINCIPAL, svc._accepted(row, with_job=True)) is None
    assert store.signed == []
