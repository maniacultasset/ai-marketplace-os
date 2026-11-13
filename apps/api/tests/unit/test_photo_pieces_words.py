"""Prod Chrome pass 12 Nov (B7): the Inbox asked "The photos seem to show 1 pieces."."""

from __future__ import annotations

from uuid import UUID

from app.core.resolved_facts import ResolvedFact
from app.services import photo_facts as pf
from mkos_schemas.jobs import PhotoObservation

IMAGE = UUID(int=1)


def _obs(value: str) -> PhotoObservation:
    return PhotoObservation(key="number_of_pieces", value=value, confidence=0.99, image_id=IMAGE)


def test_one_piece_is_singular() -> None:
    task = pf._pieces("1", None, _obs("1"))
    assert task is not None
    assert task.reason == "The photos seem to show 1 piece. Please give the number of pieces."


def test_several_pieces_stay_plural() -> None:
    task = pf._pieces("3", None, _obs("3"))
    assert task is not None and task.reason.startswith("The photos seem to show 3 pieces.")


def test_conflict_reason_is_singular_for_one() -> None:
    stored = ResolvedFact(key="number_of_pieces", value="3", source="seller")
    task = pf._pieces("1", stored, _obs("1"))
    assert task is not None
    assert task.reason == "The photos seem to show 1 piece, but the product says 3."
