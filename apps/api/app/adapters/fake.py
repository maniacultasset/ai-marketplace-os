"""`FakeAdapter`: the in-memory marketplace for the backend's own tests (CANON.md:128; S14b T10).

No network and no clock. It behaves as the adapter contract demands:
- results are stored by `idempotency_key` and replayed unchanged, even for a different body;
- a field named in `fail_fields` fails with `MARKETPLACE_UNKNOWN_ERROR`, its raw code and raw
  text kept on the item; some fields failed → `partially_published` (`API_REJECTED_PARTIAL`), all
  failed → `failed` (`PUBLISH_FAILED`), none → `published`;
- `update_listing` writes only the given fields; an unknown SKU raises `ListingNotFound`;
- `get_performance` refuses a range with `to_date < from_date`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Final
from uuid import UUID

from mkos_schemas.codes import ERROR_CODE_META, ErrorCode
from mkos_schemas.enums import ErrorSource, Marketplace, MarketplaceState, RetryClass
from mkos_schemas.errors import ErrorItem, ErrorReport
from mkos_schemas.paths import is_valid_field_path, resolve
from mkos_schemas.publish import FieldResult, ListingResult

from app.adapters.base import (
    AdapterListing,
    ListingNotFound,
    MarketplaceAdapter,
    PerformanceDay,
    submitted_fields,
)

SOURCES: Final[Mapping[Marketplace, ErrorSource]] = {
    Marketplace.AMAZON: ErrorSource.AMAZON_ADAPTER,
    Marketplace.FLIPKART: ErrorSource.FLIPKART_ADAPTER,
}
MAX_PERFORMANCE_DAYS: Final = 366


def _retryable(code: ErrorCode) -> bool:
    return ERROR_CODE_META[code].retry_class in (RetryClass.AUTO_RETRY, RetryClass.AI_CORRECT)


class FakeAdapter(MarketplaceAdapter):
    def __init__(
        self, marketplace: Marketplace | str, *, fail_fields: Mapping[str, tuple[str, str]] = {}
    ) -> None:
        self.marketplace = Marketplace(marketplace)
        self._fail = dict(fail_fields)
        self._results: dict[str, ListingResult] = {}
        self._store: dict[str, tuple[UUID, dict[str, object]]] = {}
        self._lock = asyncio.Lock()
        self.creates = 0

    # ---- inspection (tests only) ----

    def stored(self, seller_sku: str) -> dict[str, object]:
        if seller_sku not in self._store:
            raise ListingNotFound(seller_sku)
        return dict(self._store[seller_sku][1])

    # ---- results ----

    def _item(self, field: str) -> ErrorItem:
        raw_code, raw_text = self._fail[field]
        code = ErrorCode.MARKETPLACE_UNKNOWN_ERROR
        return ErrorItem(
            field=field,
            error_code=code,
            message=raw_text,
            source=SOURCES[self.marketplace],
            retryable=_retryable(code),
            retry_class=ERROR_CODE_META[code].retry_class,
            marketplace=self.marketplace,
            raw_code=raw_code,
        )

    def _result(self, item: AdapterListing, fields: Sequence[str]) -> ListingResult:
        results: list[FieldResult] = []
        failed: list[ErrorItem] = []
        for field in fields:
            if field in self._fail:
                error = self._item(field)
                failed.append(error)
                results.append(FieldResult(field=field, status="failed", error=error))
            else:
                results.append(FieldResult(field=field, status="succeeded"))
        report: ErrorReport | None = None
        if failed and len(failed) == len(results):
            status, state = "failed", MarketplaceState.REJECTED
            report = self._report("failed", ErrorCode.PUBLISH_FAILED, failed, item)
        elif failed:
            status, state = "partially_published", MarketplaceState.PARTIALLY_LIVE
            report = self._report("partial", ErrorCode.API_REJECTED_PARTIAL, failed, item)
        else:
            status, state = "published", MarketplaceState.LIVE
        return ListingResult.model_validate(
            {
                "listing_id": item.listing_id,
                "marketplace": self.marketplace,
                "external_id": f"fake-{item.seller_sku}",
                "status": status,
                "idempotency_key": item.idempotency_key,
                "fields": results,
                "marketplace_state": state,
                "error": report,
            }
        )

    def _report(
        self, status: str, code: ErrorCode, failed: list[ErrorItem], item: AdapterListing
    ) -> ErrorReport:
        return ErrorReport.model_validate(
            {
                "status": status,
                "error_code": code,
                "retryable": _retryable(code),
                "errors": failed,
                "listing_id": item.listing_id,
            }
        )

    @staticmethod
    def _values(item: AdapterListing, fields: Sequence[str]) -> dict[str, object]:
        out: dict[str, object] = {}
        for field in fields:
            value = resolve(item.listing, field)
            out[field] = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
        return out

    async def _once(
        self, item: AdapterListing, fields: Sequence[str], *, create: bool
    ) -> ListingResult:
        async with self._lock:
            replay = self._results.get(item.idempotency_key)
            if replay is not None:
                return replay
            sku = item.seller_sku
            if not create and sku not in self._store:
                raise ListingNotFound(sku)
            result = self._result(item, fields)
            written = {f: v for f, v in self._values(item, fields).items() if f not in self._fail}
            if sku in self._store:
                self._store[sku][1].update(written)
            else:
                self._store[sku] = (item.listing_id, written)
                self.creates += 1
            self._results[item.idempotency_key] = result
            return result

    # ---- the interface ----

    async def get_listing(self, seller_sku: str) -> ListingResult:
        if seller_sku not in self._store:
            raise ListingNotFound(seller_sku)
        listing_id, values = self._store[seller_sku]
        return ListingResult.model_validate(
            {
                "listing_id": listing_id,
                "marketplace": self.marketplace,
                "external_id": f"fake-{seller_sku}",
                "status": "published",
                "idempotency_key": f"get:{seller_sku}",
                "fields": [FieldResult(field=f, status="succeeded") for f in sorted(values)],
                "marketplace_state": MarketplaceState.LIVE,
            }
        )

    async def create_listing(self, item: AdapterListing) -> ListingResult:
        return await self._once(item, submitted_fields(item.listing), create=True)

    async def update_listing(self, item: AdapterListing, *, fields: Sequence[str]) -> ListingResult:
        wanted = sorted(set(fields))
        if not wanted or not all(is_valid_field_path(f) for f in wanted):
            raise ValueError("fields must be a non-empty list of legal field paths")
        return await self._once(item, wanted, create=False)

    async def publish_listing(self, item: AdapterListing) -> ListingResult:
        return await self._once(item, submitted_fields(item.listing), create=True)

    async def get_performance(
        self, listing_id: UUID, from_date: date, to_date: date
    ) -> list[PerformanceDay]:
        if to_date < from_date:
            raise ValueError("to_date is before from_date")
        days = (to_date - from_date).days + 1
        if days > MAX_PERFORMANCE_DAYS:
            raise ValueError(f"at most {MAX_PERFORMANCE_DAYS} days per call")
        return [
            PerformanceDay(
                date=from_date + timedelta(days=n),
                impressions=0,
                clicks=0,
                orders=0,
                returns=0,
                revenue=None,
            )
            for n in range(days)
        ]
