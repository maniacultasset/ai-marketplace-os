"""SI14 T4: an optional category on create, a nullable category on read, and the suggestion."""

from __future__ import annotations

import typing
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from app.api.v1.schemas.category_suggestion import CategorySuggestionAccepted
from app.api.v1.schemas.products import (
    CategorySuggestionOut,
    ProductCreate,
    ProductOut,
    ProductPatch,
)
from app.api.v1.schemas.tasks import TaskKind
from pydantic import ValidationError


def _out(**changes: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "id": str(uuid4()),
        "sku": "SKU-1",
        "title": "Cotton tee",
        "status": "draft",
        "category": None,
        "created_at": datetime.now(UTC).isoformat(),
        "updated_at": datetime.now(UTC).isoformat(),
    }
    raw.update(changes)
    return raw


def test_create_without_category() -> None:
    assert ProductCreate.model_validate({"sku": "SKU-1", "title": "Cotton tee"}).category is None


@pytest.mark.parametrize("category", ["", "T-Shirts", " "])
def test_create_refuses_a_bad_slug(category: str) -> None:
    with pytest.raises(ValidationError):
        ProductCreate.model_validate({"sku": "SKU-1", "title": "Tee", "category": category})


def test_patch_still_refuses_a_null_category() -> None:
    with pytest.raises(ValidationError):
        ProductPatch.model_validate({"category": None})


def test_out_takes_a_null_category_and_the_suggestion() -> None:
    out = ProductOut.model_validate(_out())
    assert out.category is None and out.category_suggest_enabled is False
    suggestion = CategorySuggestionOut.model_validate(
        {
            "status": "suggested",
            "candidates": [{"category": "t_shirts", "confidence": 0.9, "rationale": "A tee."}],
            "calibration_version": "v1",
            "at": datetime.now(UTC).isoformat(),
        }
    )
    out = ProductOut.model_validate(_out(suggestions={"category": suggestion.model_dump()}))
    assert out.suggestions.category == suggestion
    with pytest.raises(ValidationError):
        ProductOut.model_validate({k: v for k, v in _out().items() if k != "category"})


def test_suggestion_refuses_four_candidates_and_bad_confidence() -> None:
    one = {"category": "t_shirts", "confidence": 0.5, "rationale": "x"}
    with pytest.raises(ValidationError):
        CategorySuggestionOut.model_validate({"status": "suggested", "candidates": [one] * 4})
    with pytest.raises(ValidationError):
        CategorySuggestionOut.model_validate(
            {"status": "suggested", "candidates": [{**one, "confidence": 1.2}]}
        )


def test_accepted_and_task_kind() -> None:
    assert CategorySuggestionAccepted(product_id=uuid4(), status="unavailable").job_id is None
    assert "category_review" in typing.get_args(TaskKind)
