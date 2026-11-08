"""SI11 fix round 1 (R1-3; AC4, AC15; CP-D11, CP-D23): every Amazon repair rule is reachable from
the executor (`resolve`), not only `enum_fold`. The stored Product Type Definition is simulated
(the executor's PTD read itself is not patched), and the unit's as-built attributes are shaped
per case where the test world's product carries no such value (a measure, a dashed barcode, an
attribute the template does not send)."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource, Severity
from mkos_schemas.errors import ErrorItem

from .conftest import LOCAL_HOSTS
from .preview_support import prev, preview_jobs
from .publish_support import PubWorld, run_job
from .review_support import generated

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["prev"]

MID = "A21TJRUUN4KGV"


def _error(field: str, message: str, raw: str = "99300") -> ErrorItem:
    return ErrorItem(
        field=field,
        error_code=ErrorCode.VALUE_NOT_ALLOWED,
        severity=Severity.ERROR,
        message=message,
        source=ErrorSource.AMAZON_ADAPTER,
        retryable=False,
        raw_code=raw,
    )


def _stored_ptd(monkeypatch: pytest.MonkeyPatch, properties: dict[str, Any]) -> None:
    """A current Product Type Definition with these properties (installed after generation, so
    only the executor's read sees it)."""
    from app.repositories.product_type_definitions import ProductTypeDefinitionRepository
    from app.services import product_type_schema

    async def current(*args: Any, **kwargs: Any) -> tuple[Any, None]:
        return SimpleNamespace(definition_id=uuid.uuid4()), None

    async def schema_json(self: Any, definition_id: uuid.UUID) -> dict[str, Any]:
        return {"properties": properties}

    monkeypatch.setattr(product_type_schema, "current_schemas", current)
    monkeypatch.setattr(ProductTypeDefinitionRepository, "schema_json", schema_json)


def _shape(monkeypatch: pytest.MonkeyPatch, change: Callable[[dict[str, Any]], None]) -> None:
    from app.adapters.amazon import repairs

    real = repairs.units_as_built

    def shaped(listing: Any) -> list[tuple[Any, dict[str, Any]]]:
        out = real(listing)
        for _unit, attributes in out:
            change(attributes)
        return out

    monkeypatch.setattr(repairs, "units_as_built", shaped)


async def _run(pw: PubWorld, errors: list[ErrorItem]) -> Any:
    pw.fake.preview_errors = errors
    made = await generated(pw.world)
    (first,) = preview_jobs(pw, made.listing_id)
    return made, first


def _rules(pw: PubWorld, listing_id: uuid.UUID) -> list[tuple[Any, ...]]:
    return pw.rows(
        "SELECT rule, attribute FROM listing_repairs WHERE listing_id = %s ORDER BY created_at",
        [listing_id],
    )


async def test_unit_format_from_the_stored_definition_on_a_listing_issue(
    prev: PubWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unit comes from the PTD (no template enum), and an issue on `listing` reaches the
    attribute its text names."""
    made, first = await _run(
        prev, [_error("listing", "The value for 'item_length' must have a unit.")]
    )
    _stored_ptd(
        monkeypatch,
        {"item_length": {"items": {"properties": {"unit": {"enum": ["centimeters", "inches"]}}}}},
    )
    _shape(
        monkeypatch,
        lambda a: a.__setitem__("item_length", [{"value": "30 cm", "marketplace_id": MID}]),
    )
    await run_job(first["id"])
    assert _rules(prev, made.listing_id) == [("unit_format", "item_length")]
    assert len(preview_jobs(prev, made.listing_id)) == 2  # the re-check carries it


async def test_enum_fold_from_the_stored_definition_without_a_template_enum(
    prev: PubWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    made, first = await _run(prev, [_error("attributes.pattern", "Bad pattern.")])
    _stored_ptd(
        monkeypatch,
        {"pattern": {"items": {"properties": {"value": {"enum": ["Floral", "Solid"]}}}}},
    )
    _shape(
        monkeypatch,
        lambda a: a.__setitem__("pattern", [{"value": "floral", "marketplace_id": MID}]),
    )
    await run_job(first["id"])
    assert _rules(prev, made.listing_id) == [("enum_fold", "pattern")]


async def test_fill_from_facts_on_a_listing_issue(
    prev: PubWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    made, first = await _run(prev, [_error("listing", "'country_of_origin' is required.")])
    _shape(monkeypatch, lambda a: a.pop("country_of_origin", None))
    await run_job(first["id"])
    assert _rules(prev, made.listing_id) == [("fill_from_facts", "country_of_origin")]


async def test_identifier_normalize_through_the_barcode_alias(
    prev: PubWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    def dashed(attributes: dict[str, Any]) -> None:
        (entry,) = attributes["externally_assigned_product_identifier"]
        entry["value"] = "890831-1998998"

    made, first = await _run(prev, [_error("facts.identifier", "Bad barcode.")])
    _shape(monkeypatch, dashed)
    await run_job(first["id"])
    assert _rules(prev, made.listing_id) == [
        ("identifier_normalize", "externally_assigned_product_identifier")
    ]


async def test_a_listing_issue_naming_no_known_attribute_has_no_candidate(
    prev: PubWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `listing` fallback reads only whole attribute names in the issue's text."""
    made, first = await _run(prev, [_error("listing", "Something about colours is wrong.")])
    _stored_ptd(monkeypatch, {"color": {"items": {"properties": {"value": {"enum": ["BLUE"]}}}}})
    await run_job(first["id"])
    assert _rules(prev, made.listing_id) == []
