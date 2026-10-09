"""`FakeAdapter`: the in-memory marketplace for the backend's own tests (CANON.md:128; S14b T10).

No network and no clock. It behaves as the adapter contract demands:
- results are stored by `idempotency_key` and replayed unchanged, even for a different body;
- a field named in `fail_fields` fails with `MARKETPLACE_UNKNOWN_ERROR`, its raw code and raw
  text kept on the item; some fields failed → `partially_published` (`API_REJECTED_PARTIAL`), all
  failed → `failed` (`PUBLISH_FAILED`), none → `published`;
- an all-failed create stores nothing and counts no create, so the SKU stays unknown;
- `get_listing` derives the status from the fields the marketplace never accepted: any such field
  → `partially_published`/`PARTIALLY_LIVE`, none → `published`/`LIVE`. A failed update of a field
  that is already live keeps the old value live; an all-failed update leaves the listing as it was;
- every result holds one `ItemResult` per item (a family is one call: the parent and each
  child), and the top-level `fields` hold one result per path;
- `update_listing` writes only the given fields of the given seller SKUs (a plain list names the
  listing's own SKU); an unknown SKU raises `ListingNotFound`;
- `preview()` echoes, per item, the values a create would send; no I/O;
- `get_performance` refuses a range with `to_date < from_date`.

S21 (CP15) adds, for the publish job's tests: a `fail_fields` value may name the `ErrorCode` of
the failure as a third element (e.g. `RATE_LIMITED` for a retryable field failure; the default
stays `MARKETPLACE_UNKNOWN_ERROR`); `raise_next` (errors raised by the next mutating calls, in
order, before anything is applied); `raise_after_write` (one error raised after the next
mutating call was applied: an ambiguous answer); `calls` (every mutating call: operation and
{seller_sku: paths}); and `read_states` (what `get_listing` reads per SKU, or `gone`).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import date, timedelta
from typing import Final
from uuid import UUID

from mkos_schemas.codes import ERROR_CODE_META, ErrorCode
from mkos_schemas.enums import (
    ErrorSource,
    ItemMarketplaceState,
    Marketplace,
    MarketplaceState,
    RetryClass,
)
from mkos_schemas.errors import ErrorItem, ErrorReport
from mkos_schemas.paths import resolve
from mkos_schemas.publish import FieldResult, ItemResult, ListingResult

from app.adapters.base import (
    AdapterListing,
    AdapterPreview,
    ListingNotFound,
    MarketplaceAdapter,
    PerformanceDay,
    PreviewItem,
    UpdateFields,
    fields_by_sku,
    submitted_fields,
)

SOURCES: Final[Mapping[Marketplace, ErrorSource]] = {
    Marketplace.AMAZON: ErrorSource.AMAZON_ADAPTER,
    Marketplace.FLIPKART: ErrorSource.FLIPKART_ADAPTER,
}
MAX_PERFORMANCE_DAYS: Final = 366


def _retryable(code: ErrorCode) -> bool:
    return ERROR_CODE_META[code].retry_class in (RetryClass.AUTO_RETRY, RetryClass.AI_CORRECT)


@dataclass
class _Stored:
    listing_id: UUID
    values: dict[str, object] = dc_field(default_factory=dict)
    rejected: set[str] = dc_field(default_factory=set)  # sent, refused, no live value yet

    @property
    def status(self) -> str:
        return "partially_published" if self.rejected else "published"

    @property
    def state(self) -> MarketplaceState:
        return MarketplaceState.PARTIALLY_LIVE if self.rejected else MarketplaceState.LIVE


class FakeAdapter(MarketplaceAdapter):
    def __init__(
        self,
        marketplace: Marketplace | str,
        *,
        fail_fields: Mapping[str, tuple[str, str] | tuple[str, str, ErrorCode]] = {},
    ) -> None:
        self.marketplace = Marketplace(marketplace)
        self._fail: dict[str, tuple[str, ...]] = {k: tuple(v) for k, v in fail_fields.items()}
        self.raise_next: list[Exception] = []
        self.raise_after_write: Exception | None = None
        self.calls: list[tuple[str, dict[str, list[str]]]] = []
        # S21: what `get_listing` reads per SKU (a MarketplaceState value, or "gone")
        self.read_states: dict[str, str] = {}
        self._results: dict[str, ListingResult] = {}
        self._store: dict[str, _Stored] = {}
        self._lock = asyncio.Lock()
        self.creates = 0

    # ---- inspection (tests only) ----

    def stored(self, seller_sku: str) -> dict[str, object]:
        if seller_sku not in self._store:
            raise ListingNotFound(seller_sku)
        return dict(self._store[seller_sku].values)

    # ---- results ----

    def set_fail_fields(
        self, fail_fields: Mapping[str, tuple[str, str] | tuple[str, str, ErrorCode]]
    ) -> None:
        """Replace the failing fields (tests: a later attempt that succeeds)."""
        self._fail = {k: tuple(v) for k, v in fail_fields.items()}

    def _item(self, field: str) -> ErrorItem:
        spec = self._fail[field]
        raw_code, raw_text = spec[0], spec[1]
        code = ErrorCode(spec[2]) if len(spec) > 2 else ErrorCode.MARKETPLACE_UNKNOWN_ERROR
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

    def _field_results(self, fields: Sequence[str]) -> list[FieldResult]:
        results: list[FieldResult] = []
        for field in fields:
            if field in self._fail:
                results.append(FieldResult(field=field, status="failed", error=self._item(field)))
            else:
                results.append(FieldResult(field=field, status="succeeded"))
        return results

    def _result(self, item: AdapterListing, per_sku: Mapping[str, Sequence[str]]) -> ListingResult:
        by_path: dict[str, FieldResult] = {}
        items: list[ItemResult] = []
        for unit in item.units():
            if unit.seller_sku not in per_sku:
                continue
            unit_results = self._field_results(per_sku[unit.seller_sku])
            for one in unit_results:
                by_path.setdefault(one.field, one)
            all_failed = all(r.status == "failed" for r in unit_results)
            items.append(
                ItemResult(
                    product_id=unit.product_id,
                    seller_sku=unit.seller_sku,
                    external_id=f"fake-{unit.seller_sku}",
                    marketplace_state=(
                        ItemMarketplaceState.REJECTED if all_failed else ItemMarketplaceState.LIVE
                    ),
                    fields=unit_results,
                )
            )
        results = [by_path[path] for path in sorted(by_path)]
        failed = [r.error for r in results if r.error is not None]
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
                "items": items,
                "marketplace_state": state,
                "error": report,
                "attempt": item.attempt,
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
        self, item: AdapterListing, per_sku: Mapping[str, Sequence[str]], *, create: bool
    ) -> ListingResult:
        self.calls.append(
            ("create" if create else "update", {k: list(v) for k, v in per_sku.items()})
        )
        if self.raise_next:
            raise self.raise_next.pop(0)
        result = await self._apply(item, per_sku, create=create)
        if self.raise_after_write is not None:
            error, self.raise_after_write = self.raise_after_write, None
            raise error
        return result

    async def _apply(
        self, item: AdapterListing, per_sku: Mapping[str, Sequence[str]], *, create: bool
    ) -> ListingResult:
        async with self._lock:
            replay = self._results.get(item.idempotency_key)
            if replay is not None:
                return replay
            if not create:
                for sku in per_sku:
                    if sku not in self._store:
                        raise ListingNotFound(sku)
            result = self._result(item, per_sku)
            self._results[item.idempotency_key] = result
            if result.status == "failed":
                return result  # nothing was accepted: no new listing, an existing one unchanged
            if item.seller_sku not in self._store:
                self.creates += 1
            for sku, fields in per_sku.items():
                values = self._values(item, fields)
                written = {f: v for f, v in values.items() if f not in self._fail}
                stored = self._store.get(sku)
                if stored is None:
                    stored = self._store[sku] = _Stored(item.listing_id)
                stored.values.update(written)
                stored.rejected -= written.keys()
                stored.rejected |= {f for f in fields if f in self._fail and f not in stored.values}
            return result

    # ---- the interface ----

    async def get_listing(
        self, seller_sku: str, *, listing_id: UUID | None = None
    ) -> ListingResult:
        forced = self.read_states.get(seller_sku)
        if seller_sku not in self._store or forced == "gone":
            raise ListingNotFound(seller_sku)
        stored = self._store[seller_sku]
        return ListingResult.model_validate(
            {
                "listing_id": stored.listing_id,
                "marketplace": self.marketplace,
                "external_id": f"fake-{seller_sku}",
                "status": stored.status,
                "idempotency_key": f"get:{seller_sku}",
                "fields": [FieldResult(field=f, status="succeeded") for f in sorted(stored.values)],
                "marketplace_state": stored.state if forced is None else MarketplaceState(forced),
            }
        )

    def _all(self, item: AdapterListing) -> dict[str, list[str]]:
        fields = submitted_fields(item.listing)
        return {unit.seller_sku: fields for unit in item.units()}

    async def create_listing(self, item: AdapterListing) -> ListingResult:
        return await self._once(item, self._all(item), create=True)

    async def update_listing(self, item: AdapterListing, *, fields: UpdateFields) -> ListingResult:
        return await self._once(item, fields_by_sku(item, fields), create=False)

    def preview(self, item: AdapterListing) -> AdapterPreview:
        payload = self._values(item, submitted_fields(item.listing))
        units = item.units()
        return AdapterPreview(
            product_type=None,
            items=tuple(
                PreviewItem(u.product_id, u.seller_sku, u.item_role, dict(payload)) for u in units
            ),
            report=ErrorReport(status="passed", listing_id=item.listing_id),
        )

    async def publish_listing(self, item: AdapterListing) -> ListingResult:
        return await self._once(item, self._all(item), create=True)

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
