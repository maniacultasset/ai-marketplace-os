"""`AmazonAdapter`: the `MarketplaceAdapter` over SP-API Listings Items (spec L5272, L10529-10561;
invariants 1, 4, 8, 9, 10; S20 T11, T12).

- `create_listing`: `check()` errors block the whole listing (a `failed` result, nothing sent).
  Otherwise one PUT per item, sequentially, never concurrently (D11): a standalone item, or the
  parent then each child (a family is one unit, ADR-0007 §3). A parent that comes back INVALID
  sends no child (children `skipped`).
- Results per field and per item (D5): an ERROR issue fails the paths that feed its attributes
  (`reverse_index`); an issue naming no attribute fails `listing` or the error-map row's
  `field_hint`; a WARNING issue leaves the field `succeeded` with a warning item (C7); INFO is
  dropped. INVALID fails the named paths and skips the rest. ACCEPTED is not "live" (open
  decision 27, DW1): an accepted item is `processing`.
- C2: a known failure (429 past the budget, auth, connect, a failed GET) is raised only when
  nothing was applied. After at least one applied item the result is `partially_published`:
  the applied items keep their results, the failing item is `failed` with that code, the rest
  `skipped`, and `error` carries the code. An ambiguous failure after a write is always raised
  (`MarketplaceAmbiguous`) with the applied items in `completed`, so S21 reads before retrying.
- `update_listing(item, fields={sku: paths})`: one PATCH per named SKU, `replace` ops for exactly
  the attributes those paths feed; a path that shares its attribute (`generic_keyword`) expands
  to every path feeding it, so the full value is sent and each path is reported (C6).
- `publish_listing` is `create_listing`: Listings Items PUT is create-or-replace (C11, DW1).
- `get_listing` (S21 CP6): BUYABLE -> live; an item-level ERROR issue and no BUYABLE -> rejected;
  attribute ERROR issues -> partially live (S20); neither BUYABLE nor DISCOVERABLE -> inactive; else
  processing; 404 -> `ListingNotFound`.
- The adapter never checks approval (invariant 1: S21 calls it only after approval), never runs
  a `fix_action`, and passes `attempt` through unchanged (invariant 9).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from typing import Any, Final
from uuid import UUID

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ItemMarketplaceState, Marketplace, MarketplaceState, Severity
from mkos_schemas.errors import ErrorItem, ErrorReport
from mkos_schemas.publish import FieldResult, ItemResult, ListingResult

from app.adapters.amazon import constants as c
from app.adapters.amazon.client import SpApiClient, SpApiResponse
from app.adapters.amazon.mapping import (
    DEFAULT_MARKETPLACE_ID,
    MappedItem,
    build,
    error,
    is_mapped,
    preview,
    reverse_index,
)
from app.adapters.base import (
    AdapterError,
    AdapterItem,
    AdapterListing,
    AdapterPreview,
    ListingNotFound,
    MarketplaceAdapter,
    MarketplaceAmbiguous,
    MarketplaceUnavailable,
    NullSink,
    PerformanceDay,
    RawResponseSink,
    UpdateFields,
    fields_by_sku,
)
from app.adapters.error_map import ErrorMapper

NO_LISTING_ID: Final = UUID(int=0)
MAX_PERFORMANCE_DAYS: Final = 366
_ORDER: Final = {"failed": 0, "succeeded": 1, "skipped": 2}


class _Outcome:
    """What one item's call produced."""

    def __init__(self, item: ItemResult, *, applied: bool, invalid: bool, ref: str | None) -> None:
        self.item = item
        self.applied = applied
        self.invalid = invalid
        self.ref = ref


class AmazonAdapter(MarketplaceAdapter):
    def __init__(
        self,
        client: SpApiClient,
        error_map: ErrorMapper,
        sink: RawResponseSink | None = None,
        *,
        marketplace_id: str = DEFAULT_MARKETPLACE_ID,
    ) -> None:
        self._client = client
        self._map = error_map
        self._sink = sink or NullSink()
        self._marketplace_id = marketplace_id
        self.marketplace = Marketplace.AMAZON

    def __repr__(self) -> str:
        return "AmazonAdapter()"

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---- preview ----

    def preview(self, item: AdapterListing) -> AdapterPreview:
        return preview(item, marketplace_id=self._marketplace_id)

    # ---- issues -> field results ----

    def _issue_items(
        self,
        response: SpApiResponse,
        mapped: MappedItem,
        index: Mapping[str, tuple[str, ...]],
        scope: Iterable[str],
        values: Mapping[str, str | None],
    ) -> tuple[dict[str, ErrorItem], dict[str, ErrorItem], list[ErrorItem]]:
        """(failed path -> error, warned path -> warning, item-level errors)."""
        body = response.json if isinstance(response.json, dict) else {}
        issues = body.get("issues") or body.get("errors") or []
        in_scope = set(scope)
        failed: dict[str, ErrorItem] = {}
        warned: dict[str, ErrorItem] = {}
        loose: list[ErrorItem] = []
        for issue in issues if isinstance(issues, list) else []:
            if not isinstance(issue, dict):
                continue
            severity = str(issue.get("severity", "ERROR")).upper()
            if severity == "INFO":
                continue
            names = [str(n) for n in issue.get("attributeNames") or []]
            categories = [str(x) for x in issue.get("categories") or []]
            found: Sequence[str | None] = sorted(
                {p for n in names for p in index.get(n, ()) if p in in_scope}
            )
            for path in found or [None]:
                one = self._map.to_item(
                    raw_code=str(issue.get("code", "")),
                    raw_message=str(issue.get("message", "")),
                    field=path,
                    value=values.get(path) if path else None,
                    categories=categories,
                )
                if severity == "WARNING":
                    one = one.model_copy(update={"severity": Severity.WARNING})
                    if path is not None:
                        warned.setdefault(path, one)
                    continue
                one = one.model_copy(update={"severity": Severity.ERROR})
                if path is None:
                    loose.append(one)
                else:
                    failed.setdefault(path, one)
        return failed, warned, loose

    def _outcome(
        self,
        response: SpApiResponse,
        mapped: MappedItem,
        index: Mapping[str, tuple[str, ...]],
        scope: Sequence[str],
        sent: Sequence[str],
    ) -> _Outcome:
        unit = mapped.unit
        values: dict[str, str | None] = dict.fromkeys(scope)
        failed, warned, loose = self._issue_items(response, mapped, index, scope, values)
        body = response.json if isinstance(response.json, dict) else {}
        status = str(body.get("status", "")).upper()
        invalid = response.status != 200 or status != c.ACCEPTED
        results: list[FieldResult] = []
        for path in scope:
            if path in failed:
                results.append(FieldResult(field=path, status="failed", error=failed[path]))
            elif path not in sent or invalid:
                results.append(FieldResult(field=path, status="skipped"))
            else:
                results.append(FieldResult(field=path, status="succeeded", error=warned.get(path)))
        for one in loose:
            if all(r.field != one.field for r in results):
                results.append(FieldResult(field=one.field, status="failed", error=one))
        if invalid and not any(r.status == "failed" for r in results):
            fallback = (
                loose[0]
                if loose
                else error(
                    "listing",
                    ErrorCode.MARKETPLACE_UNKNOWN_ERROR,
                    "Amazon refused the item without naming an attribute.",
                )
            )
            results.append(FieldResult(field=fallback.field, status="failed", error=fallback))
        results.sort(key=lambda r: r.field)
        state = ItemMarketplaceState.REJECTED if invalid else ItemMarketplaceState.PROCESSING
        submission = body.get("submissionId")
        return _Outcome(
            ItemResult(
                product_id=unit.product_id,
                seller_sku=unit.seller_sku,
                marketplace_state=state,
                fields=_dedupe(results),
            ),
            applied=not invalid,
            invalid=invalid,
            ref=str(submission)[:128] if submission else None,
        )

    # ---- results ----

    def _failed_item(
        self, unit: AdapterItem, paths: Sequence[str], report: ErrorReport
    ) -> ItemResult:
        base = report.errors[0] if report.errors else None
        fields = []
        for path in paths:
            one = base.model_copy(update={"field": path}) if base else None
            fields.append(FieldResult(field=path, status="failed", error=one))
        return ItemResult(
            product_id=unit.product_id,
            seller_sku=unit.seller_sku,
            marketplace_state=ItemMarketplaceState.NOT_SUBMITTED,
            fields=fields,
        )

    @staticmethod
    def _skipped_item(unit: AdapterItem, paths: Sequence[str]) -> ItemResult:
        return ItemResult(
            product_id=unit.product_id,
            seller_sku=unit.seller_sku,
            marketplace_state=ItemMarketplaceState.NOT_SUBMITTED,
            fields=[FieldResult(field=p, status="skipped") for p in paths],
        )

    def _result(
        self,
        item: AdapterListing,
        items: list[ItemResult],
        *,
        ref: str | None,
        bodies: list[bytes],
        transport_error: ErrorReport | None = None,
    ) -> ListingResult:
        top = _aggregate(items)
        failed = [f.error for f in top if f.status == "failed" and f.error is not None]
        succeeded = any(f.status == "succeeded" for f in top)
        report: ErrorReport | None = None
        state: MarketplaceState
        if transport_error is not None:
            status, state = "partially_published", MarketplaceState.PARTIALLY_LIVE
            code = transport_error.error_code or ErrorCode.MARKETPLACE_UNAVAILABLE
            report = _report("partial", code, failed, item, retryable=transport_error.retryable)
        elif failed and not succeeded:
            status, state = "failed", MarketplaceState.REJECTED
            report = _report("failed", ErrorCode.PUBLISH_FAILED, failed, item)
        elif failed:
            status, state = "partially_published", MarketplaceState.PARTIALLY_LIVE
            report = _report("partial", ErrorCode.API_REJECTED_PARTIAL, failed, item)
        else:
            status, state = "published", MarketplaceState.PROCESSING
        digest = hashlib.sha256(b"\n".join(bodies)).hexdigest() if bodies else None
        return ListingResult(
            listing_id=item.listing_id,
            marketplace=Marketplace.AMAZON,
            status=status,
            idempotency_key=item.idempotency_key,
            fields=top,
            submission_ref=ref,
            marketplace_state=state,
            items=items,
            raw_response_digest=digest,
            error=report,
            attempt=item.attempt,
        )

    def _blocked(
        self, item: AdapterListing, mapped: list[MappedItem], errors: list[ErrorItem]
    ) -> ListingResult:
        """`check()` refused the listing: nothing is sent, every item is reported."""
        by_path: dict[str, ErrorItem] = {}
        for one in errors:
            by_path.setdefault(one.field, one)
        items: list[ItemResult] = []
        for m in mapped:
            fields = [
                (
                    FieldResult(field=p, status="failed", error=by_path[p])
                    if p in by_path
                    else FieldResult(field=p, status="skipped")
                )
                for p in m.paths
            ]
            item_level = [
                e for e in errors if e.field not in m.paths and e.severity is Severity.ERROR
            ]
            for one in item_level:
                if all(f.field != one.field for f in fields):
                    fields.append(FieldResult(field=one.field, status="failed", error=one))
            items.append(
                ItemResult(
                    product_id=m.unit.product_id,
                    seller_sku=m.unit.seller_sku,
                    marketplace_state=ItemMarketplaceState.NOT_SUBMITTED,
                    fields=sorted(fields, key=lambda f: f.field),
                )
            )
        top = _aggregate(items)
        return ListingResult(
            listing_id=item.listing_id,
            marketplace=Marketplace.AMAZON,
            status="failed",
            idempotency_key=item.idempotency_key,
            fields=top,
            marketplace_state=MarketplaceState.NOT_SUBMITTED,
            items=items,
            error=_report("failed", ErrorCode.PUBLISH_FAILED, errors, item),
            attempt=item.attempt,
        )

    # ---- the interface ----

    async def create_listing(self, item: AdapterListing) -> ListingResult:
        mapped, errors = build(item, marketplace_id=self._marketplace_id)
        if any(e.severity is Severity.ERROR for e in errors):
            return self._blocked(item, mapped, errors)
        template = item.template.template if item.template is not None else None
        index = reverse_index(template)
        results: list[ItemResult] = []
        bodies: list[bytes] = []
        ref: str | None = None
        applied = 0
        for position, one in enumerate(mapped):
            try:
                response = await self._client.put(one.unit.seller_sku, one.payload)
            except MarketplaceAmbiguous as exc:
                raise MarketplaceAmbiguous(exc.report, completed=results) from None
            except AdapterError as exc:
                if applied == 0:
                    raise type(exc)(exc.report, completed=()) from None
                results.append(self._failed_item(one.unit, one.submitted, exc.report))
                results += [self._skipped_item(m.unit, m.paths) for m in mapped[position + 1 :]]
                return self._result(
                    item, results, ref=ref, bodies=bodies, transport_error=exc.report
                )
            bodies.append(response.raw)
            self._sink.put(one.unit.seller_sku, "put", response.raw)
            outcome = self._outcome(response, one, index, one.paths, one.submitted)
            results.append(outcome.item)
            if position == 0:
                ref = outcome.ref
            if outcome.applied:
                applied += 1
            if outcome.invalid and one.unit.item_role == "parent":
                results += [self._skipped_item(m.unit, m.paths) for m in mapped[position + 1 :]]
                break
        return self._result(item, results, ref=ref, bodies=bodies)

    async def publish_listing(self, item: AdapterListing) -> ListingResult:
        return await self.create_listing(item)

    async def update_listing(self, item: AdapterListing, *, fields: UpdateFields) -> ListingResult:
        per_sku = fields_by_sku(item, fields)
        mapped, errors = build(item, marketplace_id=self._marketplace_id)
        template = item.template.template if item.template is not None else None
        index = reverse_index(template)
        by_sku = {m.unit.seller_sku: m for m in mapped}
        for sku, paths in per_sku.items():
            unknown = [p for p in paths if p not in by_sku[sku].paths]
            if unknown:
                raise ValueError("a field path this item does not carry")
        if not is_mapped(item.template):
            return self._blocked(item, [by_sku[s] for s in per_sku], errors)
        results: list[ItemResult] = []
        bodies: list[bytes] = []
        ref: str | None = None
        applied = 0
        ordered = [m for m in mapped if m.unit.seller_sku in per_sku]
        for position, one in enumerate(ordered):
            scope = _expand(per_sku[one.unit.seller_sku], index, one.paths)
            blocked = {e.field: e for e in errors if e.field in scope}
            names = sorted(
                {
                    name
                    for name, feeds in index.items()
                    if name in one.payload["attributes"]
                    and any(p in scope and p not in blocked for p in feeds)
                }
            )
            sent = [p for p in scope if p in one.submitted and p not in blocked]
            if not names:
                results.append(_local_item(one.unit, scope, blocked))
                continue
            patches = [
                {"op": "replace", "path": f"/attributes/{n}", "value": one.payload["attributes"][n]}
                for n in names
            ]
            body = {"productType": one.payload["productType"], "patches": patches}
            try:
                response = await self._client.patch(one.unit.seller_sku, body)
            except MarketplaceAmbiguous as exc:
                raise MarketplaceAmbiguous(exc.report, completed=results) from None
            except AdapterError as exc:
                if applied == 0:
                    raise type(exc)(exc.report, completed=()) from None
                results.append(self._failed_item(one.unit, sent, exc.report))
                results += [
                    self._skipped_item(m.unit, _expand(per_sku[m.unit.seller_sku], index, m.paths))
                    for m in ordered[position + 1 :]
                ]
                return self._result(
                    item, results, ref=ref, bodies=bodies, transport_error=exc.report
                )
            if response.status == 404:
                raise ListingNotFound(one.unit.seller_sku)
            bodies.append(response.raw)
            self._sink.put(one.unit.seller_sku, "patch", response.raw)
            outcome = self._outcome(response, one, index, scope, sent)
            merged = {f.field: f for f in outcome.item.fields}
            for path, problem in blocked.items():
                merged[path] = FieldResult(field=path, status="failed", error=problem)
            results.append(
                outcome.item.model_copy(
                    update={"fields": sorted(merged.values(), key=lambda f: f.field)}
                )
            )
            ref = ref or outcome.ref
            applied += int(outcome.applied)
        return self._result(item, results, ref=ref, bodies=bodies)

    async def get_listing(
        self, seller_sku: str, *, listing_id: UUID | None = None
    ) -> ListingResult:
        response = await self._client.get(seller_sku)
        if response.status == 404:
            raise ListingNotFound(seller_sku)
        body = response.json if isinstance(response.json, dict) else {}
        if response.status != 200:
            raise MarketplaceUnavailable(
                _report(
                    "failed",
                    ErrorCode.MARKETPLACE_UNAVAILABLE,
                    [
                        error(
                            "listing",
                            ErrorCode.MARKETPLACE_UNAVAILABLE,
                            "Amazon did not return the listing.",
                        )
                    ],
                    None,
                )
            )
        summaries = body.get("summaries") or []
        summary = summaries[0] if summaries and isinstance(summaries[0], dict) else {}
        states = [str(s).upper() for s in summary.get("status") or []]
        index = reverse_index(None)
        attributes = body.get("attributes") or {}
        paths = sorted({p for name in attributes for p in index.get(name, ()) if p != "variants"})
        open_errors = [
            i
            for i in body.get("issues") or []
            if isinstance(i, dict) and str(i.get("severity", "")).upper() == "ERROR"
        ]
        # S21 CP6: BUYABLE -> live; an ERROR issue and no BUYABLE -> rejected; neither BUYABLE nor
        # DISCOVERABLE -> inactive (e.g. search suppression); else processing ("Submitted")
        # an ERROR issue naming attributes is a partial item (S20 contract: a partial create reads
        # back partially live); one naming none refuses the item as a whole
        item_errors = [i for i in open_errors if not i.get("attributeNames")]
        status = "published"
        if c.BUYABLE in states:
            state = MarketplaceState.LIVE
        elif item_errors:
            status, state = "failed", MarketplaceState.REJECTED
        elif open_errors:
            status, state = "partially_published", MarketplaceState.PARTIALLY_LIVE
        elif summaries and c.DISCOVERABLE not in states:
            state = MarketplaceState.INACTIVE
        else:
            state = MarketplaceState.PROCESSING
        asin = summary.get("asin")
        return ListingResult(
            listing_id=listing_id or NO_LISTING_ID,
            marketplace=Marketplace.AMAZON,
            external_id=str(asin)[:64] if asin else None,
            status=status,
            idempotency_key=f"get:{seller_sku}"[:255],
            fields=[FieldResult(field=p, status="succeeded") for p in paths],
            marketplace_state=state,
            raw_response_digest=hashlib.sha256(response.raw).hexdigest(),
        )

    async def get_performance(
        self, listing_id: UUID, from_date: date, to_date: date
    ) -> list[PerformanceDay]:
        if to_date < from_date:
            raise ValueError("to_date is before from_date")
        if (to_date - from_date).days + 1 > MAX_PERFORMANCE_DAYS:
            raise ValueError(f"at most {MAX_PERFORMANCE_DAYS} days per call")
        raise NotImplementedError("Amazon performance data lands with S23")


# ---- helpers ----


def _dedupe(results: Iterable[FieldResult]) -> list[FieldResult]:
    seen: dict[str, FieldResult] = {}
    for one in results:
        held = seen.get(one.field)
        if held is None or _ORDER[one.status] < _ORDER[held.status]:
            seen[one.field] = one
    return [seen[k] for k in sorted(seen)]


def _aggregate(items: Iterable[ItemResult]) -> list[FieldResult]:
    """One result per path over every item: failed wins, then succeeded, then skipped."""
    return _dedupe(f for one in items for f in one.fields)


def _expand(
    paths: Sequence[str], index: Mapping[str, tuple[str, ...]], carried: Sequence[str]
) -> list[str]:
    """`paths` plus every path that feeds the same Amazon attribute (C6)."""
    out = set(paths)
    for feeds in index.values():
        if out & set(feeds):
            out |= {p for p in feeds if p in carried}
    return sorted(out)


def _local_item(
    unit: AdapterItem, scope: Sequence[str], blocked: Mapping[str, ErrorItem]
) -> ItemResult:
    fields = [
        (
            FieldResult(field=p, status="failed", error=blocked[p])
            if p in blocked
            else FieldResult(field=p, status="skipped")
        )
        for p in scope
    ]
    return ItemResult(
        product_id=unit.product_id,
        seller_sku=unit.seller_sku,
        marketplace_state=ItemMarketplaceState.NOT_SUBMITTED,
        fields=fields,
    )


def _report(
    status: str,
    code: ErrorCode,
    errors: Sequence[ErrorItem],
    item: AdapterListing | None,
    *,
    retryable: bool | None = None,
) -> ErrorReport:
    from mkos_schemas.codes import ERROR_CODE_META
    from mkos_schemas.enums import RetryClass

    if retryable is None:
        retryable = ERROR_CODE_META[code].retry_class in (
            RetryClass.AUTO_RETRY,
            RetryClass.AI_CORRECT,
        )
    data: dict[str, Any] = {
        "status": status,
        "error_code": code,
        "retryable": retryable,
        "errors": list(errors)[:500],
        "listing_id": item.listing_id if item is not None else None,
    }
    return ErrorReport.model_validate(data)
