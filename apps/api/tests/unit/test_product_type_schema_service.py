"""DW2 T10: `current_schema` reads nothing for a Flipkart or unmapped listing, logs a missing
definition, and makes a found one ready through the validator cache."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from app.adapters.amazon import schema_check
from app.adapters.base import ProductTypeSchema
from app.services.product_type_schema import current_schema


class Repo:
    def __init__(self, ref: ProductTypeSchema | None) -> None:
        self.ref = ref
        self.current_calls: list[dict[str, Any]] = []
        self.loads = 0

    async def current(self, **key: Any) -> ProductTypeSchema | None:
        self.current_calls.append(key)
        return self.ref

    async def schema_json(self, definition_id: uuid.UUID) -> dict[str, Any]:
        self.loads += 1
        return {"type": "object"}


class Uow:
    def __init__(self, ref: ProductTypeSchema | None) -> None:
        self.ptd = Repo(ref)


@pytest.fixture(autouse=True)
def _env(ci_env: Any) -> Iterator[None]:
    schema_check.clear_cache()
    yield
    schema_check.clear_cache()


@pytest.mark.parametrize(
    ("marketplace", "product_type"), [("flipkart", "KURTA"), ("amazon", None), ("amazon", "")]
)
async def test_no_read_without_an_amazon_product_type(
    marketplace: str, product_type: str | None
) -> None:
    uow = Uow(None)
    assert await current_schema(uow, marketplace=marketplace, product_type=product_type) is None
    assert uow.ptd.current_calls == []


async def test_missing_definition_is_none_and_logged(log_lines: Any) -> None:
    capture = log_lines()
    uow = Uow(None)
    assert await current_schema(uow, marketplace="amazon", product_type="KURTA") is None
    (key,) = uow.ptd.current_calls
    assert key == {
        "marketplace_id": "A21TJRUUN4KGV",
        "product_type": "KURTA",
        "requirements": "LISTING_PRODUCT_ONLY",
        "locale": "en_IN",
    }
    assert [line for line in capture.lines() if line.get("message") == "ptd.missing"]


async def test_found_definition_is_prepared_once() -> None:
    ref = ProductTypeSchema(uuid.uuid4(), "KURTA", "U1", "LISTING_PRODUCT_ONLY", True, b"\x05" * 32)
    uow = Uow(ref)
    first = await current_schema(uow, marketplace="amazon", product_type="KURTA")
    second = await current_schema(uow, marketplace="amazon", product_type="KURTA")
    assert first is not None and second is not None and first.validator is not None
    assert uow.ptd.loads == 1
