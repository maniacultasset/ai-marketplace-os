"""SI9 T7 (AC8; CP-D5): the pure adapter check on a `PreviewSource` built in memory, with the
hand-written KURTA definition (`tests/fixtures/amazon_ptd`), no database and no socket."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from app.adapters.amazon import schema_check
from app.adapters.base import AdapterPreview
from app.core.resolved_facts import (
    Commercial,
    Compliance,
    Identifier,
    Identity,
    PartyFacts,
    ProductFacts,
    ResolvedFact,
)
from app.repositories.listings import PreviewSource, SourceProduct, SourceVersion
from app.repositories.templates import PinnedTemplate
from app.services import adapter_check as check
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource, Severity
from mkos_schemas.errors import MAX_ERROR_ITEMS, ErrorItem, ErrorReport
from mkos_schemas.listing import CanonicalListing
from tests.adapters.amazon_support import listing, template
from tests.support.fake_marketplace import ptd_fixture

from .test_amazon_schema_check import ready


@pytest.fixture(autouse=True)
def _cold_cache() -> Iterator[None]:
    schema_check.clear_cache()
    yield
    schema_check.clear_cache()


def _source(body: CanonicalListing | None = None, *, marketplace: str = "amazon") -> PreviewSource:
    body = body or listing()
    pinned = template()
    return PreviewSource(
        listing_id=uuid.uuid4(),
        marketplace=marketplace,
        category="ethnic_wear_kurta_sets",
        status="validated",
        product=SourceProduct(id=uuid.uuid4(), sku="FAB-KURTA-001", variation_theme=None),
        version=SourceVersion(
            uuid.uuid4(), 1, body.model_dump(mode="json"), category_template_id=uuid.uuid4()
        ),
        template=(
            PinnedTemplate(uuid.uuid4(), 1, pinned.product_type, None, pinned.template)
            if marketplace == "amazon"
            else None
        ),
    )


def _family(source: PreviewSource) -> Any:
    """SI2: the family facts the check builds the payload from (a standalone, no children): the
    seller gave the version's attributes, a price, the MRP, the origin, the manufacturer, (SI5
    B2: an Amazon create needs it) a valid EAN-13 barcode and (SI8: so does a brand, and a Kurta
    set an audience) the brand and the gender."""
    assert source.version is not None
    attributes = CanonicalListing.model_validate(source.version.content).attributes
    main = ProductFacts(
        product_id=source.product.id,
        sku=source.product.sku,
        level="standalone",
        identity=Identity(brand_name="Test Brand", gender="women"),
        commercial=Commercial(mrp="999.00", selling_price="499.00", currency="INR"),
        identifier=Identifier(type="ean", value="4006381333931", status="provided"),
        compliance=Compliance(
            country_of_origin="IN",
            manufacturer=PartyFacts(
                legal_name="Test Mills Pvt Ltd", address="1 Mill Road, Surat, GJ, 395003, IN"
            ),
        ),
        attributes={
            key: ResolvedFact(key=key, value=value, source="seller")
            for key, value in attributes.items()
        },
    )
    return main, ()


def _check(source: PreviewSource, schema: Any) -> ErrorReport | None:
    return check.check_source(source, _family(source), schema)


def _kurta() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(ptd_fixture("KURTA.schema.json"))
    return loaded


async def test_clean_listing_passes() -> None:
    report = _check(_source(), await ready(_kurta()))
    assert report is not None and report.status == "passed" and report.errors == []


async def test_missing_required_attribute_is_one_schema_error() -> None:
    report = _check(_source(listing(fit=None)), await ready(_kurta()))
    assert report is not None and report.status == "failed"
    (item,) = report.errors
    assert (item.field, item.error_code, item.source, item.severity) == (
        "attributes.fit",
        ErrorCode.MISSING_ATTRIBUTE,
        ErrorSource.SCHEMA,
        Severity.ERROR,
    )
    assert report.error_code is ErrorCode.MISSING_ATTRIBUTE


def test_no_schema_runs_mapping_only() -> None:
    report = _check(_source(listing(fit=None)), None)
    assert report is not None and report.status == "passed"


async def test_product_type_mismatch_is_a_warning_only() -> None:
    shirt = await ready(json.loads(ptd_fixture("SHIRT.schema.json")), product_type="SHIRT")
    report = _check(_source(), shirt)
    assert report is not None and report.status == "passed"
    (item,) = report.errors
    assert item.severity is Severity.WARNING and item.source is ErrorSource.SCHEMA


def test_flipkart_items_are_schema_items() -> None:
    report = _check(_source(marketplace="flipkart"), None)
    assert report is not None
    assert all(item.source is ErrorSource.SCHEMA for item in report.errors)


def test_previewer_raising_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_: Any) -> AdapterPreview:
        raise RuntimeError("bug")

    monkeypatch.setattr(check, "previewer", lambda _m: boom)
    report = _check(_source(), None)
    assert report is not None and report.status == "failed"
    (item,) = report.errors
    assert (item.field, item.error_code, item.source, item.message) == (
        "listing",
        ErrorCode.INTERNAL_ERROR,
        ErrorSource.SCHEMA,
        "The marketplace check could not run.",
    )


def test_item_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    many = [
        ErrorItem(
            field="attributes.color",
            error_code=ErrorCode.VALUE_NOT_ALLOWED,
            message=f"bad {i}",
            source=ErrorSource.AMAZON_ADAPTER,
            retryable=False,
        )
        for i in range(MAX_ERROR_ITEMS)
    ]
    report = ErrorReport.model_construct(status="failed", errors=[*many, *many[:10]])
    monkeypatch.setattr(check, "previewer", lambda _m: lambda _l: AdapterPreview(None, (), report))
    got = _check(_source(), None)
    assert got is not None and got.status == "failed" and len(got.errors) == MAX_ERROR_ITEMS


def test_no_previewer_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def none(_m: str) -> Any:
        raise NotImplementedError

    monkeypatch.setattr(check, "previewer", none)
    assert _check(_source(), None) is None


class _Uow:
    def __init__(self, source: PreviewSource | None) -> None:
        self.listings = self
        self.ptd = self
        self.product_facts = self
        self._source = source
        self.resolves = True
        self.seen: dict[str, Any] = {}

    async def preview_source(
        self, listing_id: uuid.UUID, *, version: SourceVersion | None = None
    ) -> PreviewSource | None:
        self.seen["version"] = version
        return self._source

    async def resolve_family(self, product_id: uuid.UUID) -> Any:
        if not self.resolves or self._source is None:
            return None
        return _family(self._source)

    async def current(self, **_: Any) -> Any:
        raise ValueError("an unreadable stored definition")


async def test_definition_read_failure_fails_closed() -> None:
    uow = _Uow(_source())
    report = await check.adapter_check(uow, uuid.uuid4())
    assert report is not None and report.errors[0].error_code is ErrorCode.INTERNAL_ERROR


async def test_no_listing_or_version_is_none() -> None:
    assert await check.adapter_check(_Uow(None), uuid.uuid4()) is None
    bare = _source()
    bare = PreviewSource(
        **{**{f: getattr(bare, f) for f in bare.__dataclass_fields__}, "version": None}
    )
    assert await check.adapter_check(_Uow(bare), uuid.uuid4()) is None


async def test_version_is_passed_through() -> None:
    uow = _Uow(None)
    version = SourceVersion(uuid.uuid4(), 2, {}, None)
    await check.adapter_check(uow, uuid.uuid4(), version=version)
    assert uow.seen["version"] is version


async def test_product_that_no_longer_resolves_fails_closed() -> None:
    plain = _source()
    plain = PreviewSource(
        **{**{f: getattr(plain, f) for f in plain.__dataclass_fields__}, "template": None}
    )
    uow = _Uow(plain)
    uow.resolves = False
    report = await check.adapter_check(uow, uuid.uuid4())
    assert report is not None and report.status == "failed"
    assert report.errors[0].error_code is ErrorCode.INTERNAL_ERROR
