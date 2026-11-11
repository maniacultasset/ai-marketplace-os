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
