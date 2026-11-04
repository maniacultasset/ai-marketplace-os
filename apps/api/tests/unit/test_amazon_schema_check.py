"""DW2 T5 (AC8, AC10, AC11): the pure PTD schema check and its wiring into `mapping.build()`.
The schemas are the hand-written fixtures in `tests/fixtures/amazon_ptd`, made ready through
`prepare` with an in-memory loader (no database, no socket)."""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import time
import uuid
from collections.abc import Iterator, Mapping
from typing import Any

import pytest
from app.adapters.amazon import mapping, schema_check
from app.adapters.amazon.definitions import canonical_digest
from app.adapters.base import AdapterListing, ProductTypeSchema
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import Severity
from tests.adapters.amazon_support import amazon_family, amazon_item, listing
from tests.adapters.contract import no_sockets
from tests.support.fake_marketplace import ptd_fixture


def kurta() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(ptd_fixture("KURTA.schema.json"))
    return loaded


class Loader:
    def __init__(self, schemas: Mapping[uuid.UUID, Mapping[str, Any]]) -> None:
        self.schemas = dict(schemas)
        self.calls = 0

    async def __call__(self, definition_id: uuid.UUID) -> Mapping[str, Any]:
        self.calls += 1
        return self.schemas[definition_id]


@pytest.fixture(autouse=True)
def _cold_cache() -> Iterator[None]:
    schema_check.clear_cache()
    yield
    schema_check.clear_cache()


def ref_for(
    schema: Mapping[str, Any], *, enforced: bool = True, product_type: str = "KURTA"
) -> ProductTypeSchema:
    return ProductTypeSchema(
        definition_id=uuid.uuid4(),
        product_type=product_type,
        product_type_version="UFAKE0001",
        requirements="LISTING_PRODUCT_ONLY",
        enforced=enforced,
        digest=canonical_digest(schema),
    )


async def ready(schema: Mapping[str, Any], **over: Any) -> ProductTypeSchema:
    ref = ref_for(schema, **over)
    return await schema_check.prepare(ref, Loader({ref.definition_id: schema}))


def built(sent: AdapterListing, schema: ProductTypeSchema | None) -> tuple[list[Any], list[Any]]:
    return mapping.build(dataclasses.replace(sent, product_type_schema=schema))


def schema_errors(errors: list[Any]) -> list[Any]:
    return [e for e in errors if (e.raw_code or "").startswith("ptd:")]


# ---- 1-8: the AC8 cases ----


async def test_valid_kurta_has_no_errors() -> None:
    schema = await ready(kurta())
    _, errors = built(amazon_item(), schema)
    assert errors == []
    report = mapping.preview(dataclasses.replace(amazon_item(), product_type_schema=schema)).report
    assert report.status == "passed"


async def test_missing_required_mapped_attribute() -> None:
    schema = await ready(kurta())
    _, errors = built(amazon_item(listing(fit=None)), schema)
    (error,) = errors
    assert (error.field, error.error_code) == ("attributes.fit", ErrorCode.MISSING_ATTRIBUTE)
    assert error.raw_code == "ptd:required:UFAKE0001"
    assert "fit_type" in error.message and "UFAKE0001" in error.message


async def test_item_name_too_long() -> None:
    body = amazon_item().listing
    long = body.model_copy(update={"content": body.content.model_copy(update={"title": "K" * 201})})
    _, errors = built(amazon_item(long), await ready(kurta()))
    (error,) = errors
    assert (error.field, error.error_code) == ("content.title", ErrorCode.FIELD_TOO_LONG)
    assert "200" in error.message


async def test_bad_enum_value() -> None:
    schema = kurta()
    schema["properties"]["neck_style"]["items"]["properties"]["value"]["enum"] = ["Boat Neck"]
    _, errors = built(amazon_item(listing(neckline="round")), await ready(schema))
    (error,) = errors
    assert (error.field, error.error_code) == ("attributes.neckline", ErrorCode.VALUE_NOT_ALLOWED)


async def test_keyword_byte_budget_fails_both_paths() -> None:
    schema = kurta()
    schema["properties"]["generic_keyword"]["items"]["properties"]["value"][
        "maxUtf8ByteLength"
    ] = 10
    body = amazon_item().listing
    words = body.keywords.model_copy(update={"search_terms": ["कुर्ता"], "hidden_terms": ["kurta"]})
    _, errors = built(amazon_item(body.model_copy(update={"keywords": words})), await ready(schema))
    assert sorted((e.field, e.error_code) for e in errors) == [
        ("keywords.hidden_terms", ErrorCode.BYTE_BUDGET_EXCEEDED),
        ("keywords.search_terms", ErrorCode.BYTE_BUDGET_EXCEEDED),
    ]


async def test_unfed_required_attribute_goes_on_listing() -> None:
    schema = kurta()
    schema["required"] = [*schema["required"], "brand"]
    _, errors = built(amazon_item(), await ready(schema))
    (error,) = errors
    assert (error.field, error.error_code) == ("listing", ErrorCode.MISSING_ATTRIBUTE)
    assert "brand" in error.message


async def test_no_message_holds_a_submitted_value() -> None:
    schema = kurta()
    schema["properties"]["neck_style"]["items"]["properties"]["value"]["enum"] = ["Boat Neck"]
    schema["properties"]["item_name"]["items"]["properties"]["value"]["maxLength"] = 3
    schema["properties"]["item_name"]["items"]["properties"]["value"]["pattern"] = "^[a-z]+$"
    body = listing(neckline="round")
    title = "कुर्ता Zq9 title"
    body = body.model_copy(update={"content": body.content.model_copy(update={"title": title})})
    _, errors = built(amazon_item(body), await ready(schema))
    assert errors
    for error in errors:
        assert error.value_submitted is None
        for value in (title, "कुर्ता", "Zq9", "Round Neck"):
            assert value not in error.message


async def test_not_enforced_downgrades_required_only() -> None:
    schema = kurta()
    schema["required"] = [*schema["required"], "brand"]
    schema["properties"]["item_name"]["items"]["properties"]["value"]["maxLength"] = 3
    ready_schema = await ready(schema, enforced=False)
    _, errors = built(amazon_item(listing(fit=None)), ready_schema)
    by_code = {(e.field, e.error_code): e.severity for e in errors}
    assert by_code[("attributes.fit", ErrorCode.MISSING_ATTRIBUTE)] is Severity.WARNING
    assert by_code[("listing", ErrorCode.MISSING_ATTRIBUTE)] is Severity.WARNING
    assert by_code[("content.title", ErrorCode.FIELD_TOO_LONG)] is Severity.ERROR
    _, required_only = built(amazon_item(listing(fit=None)), await ready(kurta(), enforced=False))
    report = mapping.report_for(required_only, amazon_item())
    assert report.status == "passed"


# ---- 9-15 ----


async def test_external_ref_is_one_schema_invalid_without_a_socket() -> None:
    schema = kurta()
    schema["properties"]["item_name"] = {"$ref": "https://schemas.example.invalid/x.json#/a"}
    with no_sockets():
        _, errors = built(amazon_item(), await ready(schema))
    (error,) = errors
    assert (error.field, error.error_code) == ("listing", ErrorCode.SCHEMA_INVALID)
    assert error.severity is Severity.ERROR


def _wide_schema(n: int) -> dict[str, Any]:
    schema = kurta()
    schema["required"] = [*schema["required"], *(f"unfed_{i:03d}" for i in range(n))]
    return schema


@pytest.mark.parametrize(("violations", "expected"), [(60, 51), (50, 50), (0, 0)])
async def test_error_cap(violations: int, expected: int) -> None:
    schema = await ready(_wide_schema(violations))
    items = schema_check.check_item(
        mapping.build(amazon_item())[0][0].payload["attributes"], schema, {}
    )
    assert len(items) == expected
    if violations > 50:
        summary = items[-1]
        assert summary.error_code is ErrorCode.SCHEMA_INVALID and "10 more" in summary.message


async def test_family_parent_not_flagged_for_child_only_requirements() -> None:
    schema = await ready(kurta())
    items, errors = built(amazon_family(), schema)
    assert errors == []
    assert items[0].unit.item_role == "parent"
    # the parent sends no image and no colour: the fixture asks them of non-parents only
    assert "main_product_image_locator" not in items[0].payload["attributes"]
    broken = kurta()
    broken["properties"]["size"]["items"]["properties"]["value"]["enum"] = ["XS"]
    _, errors = built(amazon_family(), await ready(broken))
    assert errors and all(e.error_code is ErrorCode.VALUE_NOT_ALLOWED for e in errors)
    # the same violation on several children is reported once per field
    assert len({(e.field, e.message) for e in errors}) == len(errors)


async def test_validator_cache() -> None:
    schema = kurta()
    ref = ref_for(schema)
    loader = Loader({ref.definition_id: schema})
    first = await schema_check.prepare(ref, loader)
    again = await schema_check.prepare(ref, loader)
    assert loader.calls == 1 and first.validator is again.validator
    other = kurta()
    other["title"] = "another digest"
    other_ref = ref_for(other)
    loader.schemas[other_ref.definition_id] = other
    await schema_check.prepare(other_ref, loader)
    assert loader.calls == 2
    for i in range(16):
        extra = kurta()
        extra["title"] = f"digest {i}"
        extra_ref = ref_for(extra)
        loader.schemas[extra_ref.definition_id] = extra
        await schema_check.prepare(extra_ref, loader)
    calls = loader.calls
    await schema_check.prepare(ref, loader)  # evicted: loaded again
    assert loader.calls == calls + 1
    bad = kurta()
    bad["properties"]["item_name"] = {"$ref": "https://schemas.example.invalid/x.json"}
    bad_ref = ref_for(bad)
    loader.schemas[bad_ref.definition_id] = bad
    await schema_check.prepare(bad_ref, loader)
    await schema_check.prepare(bad_ref, loader)
    assert loader.calls == calls + 3  # a compile failure is never cached
    unprepared = ref_for(schema)
    (error,) = schema_check.check_item({}, unprepared, {})
    assert (error.field, error.error_code) == ("listing", ErrorCode.SCHEMA_INVALID)


async def test_product_type_mismatch_is_one_warning() -> None:
    schema = await ready(json.loads(ptd_fixture("SHIRT.schema.json")), product_type="SHIRT")
    _, errors = built(amazon_item(listing(fit=None)), schema)
    (error,) = errors
    assert error.error_code is ErrorCode.SCHEMA_INVALID and error.severity is Severity.WARNING
    assert error.raw_code is None


async def test_empty_attributes_and_empty_schema() -> None:
    schema = await ready(kurta())
    items = schema_check.check_item({}, schema, {})
    assert {e.error_code for e in items} == {ErrorCode.MISSING_ATTRIBUTE}
    empty = await ready({})
    assert schema_check.check_item({}, empty, {}) == []


async def test_nan_is_refused_upstream_and_fails_closed_here() -> None:
    from mkos_schemas.listing import CanonicalListing
    from pydantic import ValidationError

    body = listing(number_of_pieces=float("nan"))
    # A real listing is a validated wire model, and the model refuses NaN.
    with pytest.raises(ValidationError):
        CanonicalListing.model_validate(body.model_dump())
    # `_number` passes a float through, so an unvalidated NaN does reach the payload: the check
    # reports one SCHEMA_INVALID on the field and never crashes (DEVIATIONS.md, T5-15).
    _, errors = built(amazon_item(body), await ready(kurta()))
    (error,) = errors
    assert (error.field, error.error_code) == (
        "attributes.number_of_pieces",
        ErrorCode.SCHEMA_INVALID,
    )


# ---- 16: no definition, no change (AC10) ----


@pytest.mark.parametrize(
    "sent",
    [
        amazon_item(),
        amazon_item(listing(fit=None)),
        amazon_item(listing(color=None)),
        amazon_item(listing(neckline="hexagon")),
        amazon_family(),
        amazon_family(theme="weird"),
        amazon_item(template=None),
    ],
)
def test_no_definition_no_change(sent: AdapterListing) -> None:
    assert sent.product_type_schema is None
    assert mapping.build(sent) == mapping.build(dataclasses.replace(sent, product_type_schema=None))


# ---- 17-19: the merge in build() ----


async def test_three_unfed_required_are_three_errors() -> None:
    schema = kurta()
    schema["required"] = [*schema["required"], "brand", "department", "target_gender"]
    _, errors = built(amazon_item(), await ready(schema))
    assert sorted(e.message.split(":")[0] for e in errors) == [
        "Amazon brand",
        "Amazon department",
        "Amazon target_gender",
    ]
    assert all((e.field, e.error_code) == ("listing", ErrorCode.MISSING_ATTRIBUTE) for e in errors)


async def test_template_required_reported_once() -> None:
    # `color` is required by both the template and the fixture schema (for non-parents)
    _, errors = built(amazon_item(listing(color=None)), await ready(kurta()))
    (error,) = errors
    assert (error.field, error.error_code) == ("attributes.color", ErrorCode.MISSING_ATTRIBUTE)
    assert error.raw_code is None  # the template check's, not the schema's


async def test_cap_summary_survives_the_merge_and_families_dedupe() -> None:
    _, errors = built(amazon_item(), await ready(_wide_schema(60)))
    found = schema_errors(errors)
    assert len(found) == 51
    assert found[-1].raw_code == "ptd:summary:UFAKE0001"
    broken = kurta()
    broken["properties"]["fit_type"]["items"]["properties"]["value"]["enum"] = ["Slim"]
    _, errors = built(amazon_family(), await ready(broken))
    assert [e.field for e in errors] == ["attributes.fit"]


# ---- L8: a catastrophic pattern ----


async def test_catastrophic_pattern_is_bounded() -> None:
    schema = kurta()
    schema["properties"]["item_name"]["items"]["properties"]["value"]["pattern"] = r"^(a|a)+\1$"
    body = amazon_item().listing
    title = "a" * 150 + "c"
    adversarial = body.model_copy(
        update={"content": body.content.model_copy(update={"title": title})}
    )
    started = time.monotonic()
    _, errors = built(amazon_item(adversarial), await ready(schema))
    assert time.monotonic() - started < 1.0
    (error,) = errors
    assert (error.field, error.error_code) == ("content.title", ErrorCode.SCHEMA_INVALID)


def test_fixture_digest_is_stable() -> None:
    schema = kurta()
    assert canonical_digest(schema) == canonical_digest(copy.deepcopy(schema))
    assert len(hashlib.sha256(ptd_fixture("KURTA.schema.json")).digest()) == 32


def test_compile_options_are_offline_and_masked(monkeypatch: pytest.MonkeyPatch) -> None:
    # jsonschema-rs fetches in Rust, past Python's socket guard, so the options are pinned here.
    import jsonschema_rs

    seen: dict[str, Any] = {}
    real = jsonschema_rs.Draft201909Validator

    def spy(schema: Any, **options: Any) -> Any:
        seen.update(options, schema=schema)
        return real(schema, **options)

    monkeypatch.setattr(jsonschema_rs, "Draft201909Validator", spy)
    schema_check.compile_schema(kurta())
    assert seen["offline"] is True and "retriever" not in seen and "registry" not in seen
    assert seen["validate_formats"] is False and seen["mask"] == schema_check.MASK
    assert "$schema" not in seen["schema"]


async def test_messages_are_built_from_the_schema_not_the_library() -> None:
    schema = kurta()
    schema["properties"]["item_name"]["items"]["properties"]["value"]["maxLength"] = 3
    schema["properties"]["neck_style"]["items"]["properties"]["value"]["enum"] = ["Boat Neck"]
    _, errors = built(amazon_item(listing(neckline="round")), await ready(schema))
    assert errors
    for error in errors:
        assert error.message.startswith("Amazon ")
        assert schema_check.MASK not in error.message


# ---- review round 1 regressions ----


def _five() -> AdapterListing:
    base = listing()
    body = base.model_copy(
        update={
            "attributes": {"color": "navy blue", "fabric_type": "cotton"},
            "keywords": base.keywords.model_copy(update={"search_terms": [], "hidden_terms": []}),
            "images": [],
        }
    )
    return amazon_item(body)


@pytest.mark.parametrize("enforced", [True, False])
async def test_update_patch_is_not_blocked_by_a_schema_error(enforced: bool) -> None:
    """PLAN D5 / AC9: an update PATCH is never withheld for a schema error (an ERROR, or a
    WARNING when NOT_ENFORCED); Amazon's own answer decides."""
    from tests.adapters.amazon_support import SELLER, amazon_adapter
    from tests.support.fake_marketplace import build_fake

    app = build_fake()
    adapter = amazon_adapter(app)
    sent = _five()
    await adapter.create_listing(sent)
    schema = kurta()
    schema["properties"]["item_name"]["items"]["properties"]["value"]["maxLength"] = 5
    prepared = await ready(schema, enforced=enforced)
    body = sent.listing
    changed = body.model_copy(
        update={"content": body.content.model_copy(update={"title": "A longer new title"})}
    )
    update = dataclasses.replace(
        sent, listing=changed, idempotency_key="update-1", product_type_schema=prepared
    )
    assert any(e.field == "content.title" for e in schema_errors(built(update, prepared)[1]))
    result = await adapter.update_listing(update, fields=["content.title"])
    after = app.state.fake.spapi_items[(SELLER, sent.seller_sku)]["attributes"]
    statuses = {f.field: f.status for f in result.fields}
    assert after["item_name"][0]["value"] == "A longer new title", statuses
    assert statuses["content.title"] == "succeeded"


def _deep_schema(depth: int = 300) -> dict[str, Any]:
    # 600 nested objects: json.loads and the refresh parser accept it, copy.deepcopy does not
    from app.adapters.amazon import definitions

    body = (('{"properties":{"a":' * depth) + "{}" + ("}}" * depth)).encode()
    schema: dict[str, Any] = definitions.parse_schema(body)
    assert definitions.canonical_digest(schema)  # storable by `templates refresh`
    return schema


async def test_a_deeply_nested_schema_fails_closed() -> None:
    prepared = await ready(_deep_schema())
    items = schema_check.check_item({}, prepared, {})
    assert [i.error_code for i in items] == [ErrorCode.SCHEMA_INVALID]
    assert [i.field for i in items] == ["listing"]


def test_compile_reports_recursion_as_unusable(monkeypatch: pytest.MonkeyPatch) -> None:
    import jsonschema_rs

    def deep(schema: Any, **options: Any) -> Any:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(jsonschema_rs, "Draft201909Validator", deep)
    compiled = schema_check.compile_schema(kurta())
    assert isinstance(compiled, schema_check._CompileFailed)
