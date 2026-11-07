"""SI3 wire models: the task answer body, the confirm list and the `tasks_answer` operation."""

from __future__ import annotations

import math
from typing import Any

import pytest
from app.api.v1.schemas.products import PhotoFactOut, ProductOut, ProductPatch
from app.api.v1.schemas.tasks import TaskAnswerIn
from app.api.v1.surface import OPS, declared_responses
from app.core.rbac import Action
from mkos_schemas.codes import ErrorCode
from pydantic import ValidationError

E = ErrorCode


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"value": "V-neck", "skip": True},
        {"skip": False},
        {"value": None},
        {"value": None, "skip": True},
        {"value": math.nan},
        {"value": math.inf},
        {"value": "x" * 2001},
        {"value": "a\x00b"},
        {"value": {"nested": 1}},
        {"value": "V-neck", "extra": 1},
    ],
)
def test_task_answer_refuses(body: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        TaskAnswerIn.model_validate(body)


@pytest.mark.parametrize(
    ("body", "value", "skip"),
    [
        ({"value": "V-neck"}, "V-neck", None),
        ({"value": 3}, 3, None),
        ({"skip": True}, None, True),
        ({"value": "x" * 2000}, "x" * 2000, None),
    ],
)
def test_task_answer_accepts(body: dict[str, Any], value: object, skip: object) -> None:
    parsed = TaskAnswerIn.model_validate(body)
    assert parsed.value == value and parsed.skip == skip


@pytest.mark.parametrize(
    "confirm",
    [[], [f"k{i}" for i in range(33)], ["neckline", "neckline"], ["Neckline"], None],
)
def test_confirm_attributes_refuses(confirm: object) -> None:
    with pytest.raises(ValidationError):
        ProductPatch.model_validate({"confirm_attributes": confirm})


def test_confirm_attributes_accepts() -> None:
    patch = ProductPatch.model_validate({"confirm_attributes": ["neckline", "fit"]})
    assert patch.confirm_attributes == ["neckline", "fit"]
    assert ProductPatch.model_validate({}).confirm_attributes is None


def test_product_out_photo_facts() -> None:
    assert ProductOut.model_fields["photo_facts"].default_factory is not None
    fact = PhotoFactOut.model_validate(
        {
            "key": "neckline",
            "value": "v-neck",
            "confidence": 0.95,
            "image_id": "0b1c2d3e-0000-4000-8000-000000000501",
            "confirmed": False,
            "confirmed_at": None,
        }
    )
    assert fact.confirmed is False


def test_tasks_answer_operation() -> None:
    op = OPS["tasks_answer"]
    assert (op.method, op.path, op.status) == ("POST", "/tasks/{task_id}/answer", 200)
    assert op.idempotency and op.has_body and op.path_id == "task_id"
    assert op.action is Action.PRODUCT_IMPORT
    codes = op.error_codes
    assert codes[409] == (E.IDEMPOTENCY_KEY_REUSED, E.INVALID_STATUS_TRANSITION)
    assert E.FACT_LEVEL_INVALID in codes[422] and E.UNKNOWN_FACT_KEY in codes[422]
    assert codes[404] == (E.NOT_FOUND,)
    assert {401, 403, 404, 409, 422} <= set(declared_responses(op))
    keys = list(OPS)
    assert keys.index("tasks_answer") == keys.index("tasks_list") + 1
