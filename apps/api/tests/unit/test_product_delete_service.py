"""PD1: the product delete's pure parts: the per-item answer of a refusal, the bulk body, and the
refusal of a listing the marketplace has seen (no database)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from app.api.v1.schemas.product_delete import MAX_BULK_DELETE, ProductBulkDeleteRequest
from app.core.errors import ConflictError, InputError, NotFoundError
from app.services import product_delete
from mkos_schemas.codes import ErrorCode
from pydantic import ValidationError


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (NotFoundError("product_id"), (404, ErrorCode.NOT_FOUND)),
        (InputError(ErrorCode.VALIDATION_FAILED, "product_id"), (422, ErrorCode.VALIDATION_FAILED)),
        (
            ConflictError(ErrorCode.INVALID_STATUS_TRANSITION, "listing_published"),
            (409, ErrorCode.INVALID_STATUS_TRANSITION),
        ),
        (ConflictError(ErrorCode.LISTING_BUSY, "listings"), (409, ErrorCode.LISTING_BUSY)),
        (
            ConflictError(ErrorCode.SUBMISSION_IN_FLIGHT, "listing_publishing"),
            (409, ErrorCode.SUBMISSION_IN_FLIGHT),
        ),
        (ConflictError(ErrorCode.INTERNAL_ERROR, "x"), (500, ErrorCode.INTERNAL_ERROR)),
    ],
)
def test_refusal_maps_each_domain_error(exc: Any, expected: tuple[int, ErrorCode]) -> None:
    assert product_delete._refusal(exc) == expected


@pytest.mark.parametrize(
    "code",
    [
        ErrorCode.NOT_FOUND,
        ErrorCode.INVALID_STATUS_TRANSITION,
        ErrorCode.SUBMISSION_IN_FLIGHT,
        ErrorCode.LISTING_BUSY,
        ErrorCode.VALIDATION_FAILED,
    ],
)
def test_item_report_names_the_code_and_never_a_value(code: ErrorCode) -> None:
    report = product_delete._report(code)
    assert report.status == "failed" and report.error_code is code
    [item] = report.errors
    assert item.field == "request" and item.error_code is code
    assert item.message and "{" not in item.message


def test_bulk_body_bounds() -> None:
    one = uuid.uuid4()
    assert ProductBulkDeleteRequest(product_ids=[one]).product_ids == [one]
    for bad in ([], [one, one], [uuid.uuid4() for _ in range(MAX_BULK_DELETE + 1)]):
        with pytest.raises(ValidationError):
            ProductBulkDeleteRequest(product_ids=bad)
    with pytest.raises(ValidationError):
        ProductBulkDeleteRequest.model_validate({"product_ids": [str(one)], "extra": 1})


class _Uow:
    """Just enough of a unit of work for `_check`."""

    def __init__(self, product: Any, listings: list[Any], submitted: bool = False) -> None:
        self.products = SimpleNamespace(
            get_for_update=self._value(product), live_child_ids=self._value([])
        )
        self.listings = SimpleNamespace(for_product_for_update=self._value(listings))
        self.submissions = SimpleNamespace(latest=self._value(object() if submitted else None))

    @staticmethod
    def _value(value: Any) -> Any:
        async def call(*_: Any) -> Any:
            return value

        return call


def _listing(**over: Any) -> Any:
    values = {
        "id": uuid.uuid4(),
        "status": "validated",
        "first_published_at": None,
        "marketplace_state": "not_submitted",
    }
    return SimpleNamespace(**{**values, **over})


def _product(**over: Any) -> Any:
    return SimpleNamespace(**{"id": uuid.uuid4(), "parent_product_id": None, **over})


@pytest.mark.parametrize(
    ("listing", "submitted", "code"),
    [
        (_listing(status="published"), False, ErrorCode.INVALID_STATUS_TRANSITION),
        (_listing(status="publishing"), False, ErrorCode.SUBMISSION_IN_FLIGHT),
        (
            _listing(status="failed", first_published_at=1),
            False,
            ErrorCode.INVALID_STATUS_TRANSITION,
        ),
        (_listing(marketplace_state="rejected"), False, ErrorCode.INVALID_STATUS_TRANSITION),
        (_listing(), True, ErrorCode.INVALID_STATUS_TRANSITION),
    ],
)
async def test_check_refuses_a_listing_the_marketplace_saw(
    listing: Any, submitted: bool, code: ErrorCode
) -> None:
    uow: Any = _Uow(_product(), [listing], submitted)
    with pytest.raises(ConflictError) as raised:
        await product_delete._check(uow, uuid.uuid4())
    assert raised.value.code is code


async def test_check_passes_an_unsent_listing_and_refuses_a_size() -> None:
    style = _product()
    plan = await product_delete._check(_Uow(style, [_listing()]), style.id)  # type: ignore[arg-type]
    assert plan.family == [style.id] and len(plan.listings) == 1
    with pytest.raises(InputError):
        await product_delete._check(_Uow(_product(parent_product_id=uuid.uuid4()), []), style.id)  # type: ignore[arg-type]
    with pytest.raises(NotFoundError):
        await product_delete._check(_Uow(None, []), style.id)  # type: ignore[arg-type]
