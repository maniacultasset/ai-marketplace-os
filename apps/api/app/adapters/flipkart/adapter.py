"""`FlipkartAdapter`: the `MarketplaceAdapter` over Flipkart listings v3 (spec L10573-10575,
L5300-5304, L6778; invariants 1, 4, 8, 9, 10; S22 T11, T12). Believed wire shapes, DW1.

- `create_listing`: `check()` errors block the whole listing (a `failed` result, nothing sent).
  Otherwise the items go in chunks of `CHUNK` per request, sequentially: a standalone item, or a
  family's children (no parent row: DATABASE.md:471, :754-755; T45). One `ListingResult` with one
  `ItemResult` per item; each item carries its Flipkart `listing_id` as `external_id`, and the
  listing's own `external_id` is that id for a standalone item, None for a family (DATABASE.md:754).
  `submission_ref` is the first request's `request_id` (DATABASE.md:863).
- Results per field and per SKU: an ERROR naming an attribute fails the paths that feed it
  (`reverse_index`); an error naming no attribute fails `listing` or the error-map row's
  `field_hint`; a WARNING leaves the field `succeeded` with a warning item (S20 C7). A SKU
  answered `FAILURE` fails the named paths and skips the rest. Some failed while some item applied
  a field -> `partially_published` + API_REJECTED_PARTIAL (judged per item, so one refused family
  child never fails the children Flipkart created, S22b F4); nothing applied -> `failed` +
  PUBLISH_FAILED. Raw codes and text are kept, mapped
  through the error map to the one `ErrorItem` (`source=flipkart_adapter`).
- S20 C2: a known failure (429 past the budget, auth, connect) is raised only when nothing was
  applied. After a chunk was applied the result is `partially_published`: applied items keep
  their results, the failing chunk's items are `failed` with that code, later chunks `skipped`.
  An ambiguous failure after a write is always raised (`MarketplaceAmbiguous`) with the applied
  items in `completed`, so S21 reads before retrying.
- `update_listing(item, fields={sku: paths})`: one update request (chunked) carrying exactly the
  attributes those paths feed for the named SKUs; a path sharing its attribute (`keywords`,
  `images`) expands to every path feeding it, so the full value is sent and each path reported.
  A SKU Flipkart never created (its failed path is `listing` or `variants[<sku>]`, which a SKU
  refused at create always carries, or the update answers LISTING_NOT_FOUND while another SKU
  of the request was found) is sent whole through the create route in the same attempt and
  merged into the same result (S22b F3). An update whose every SKU is unknown, with no SKU
  marked not created, raises `ListingNotFound` (the adapter contract).
- `publish_listing` is `create_listing` (create is an upsert by seller SKU, believed; DW1).
- `get_listing`: ACTIVE -> live, else processing; open errors -> partially live; unknown SKU ->
  `ListingNotFound`.
- The adapter never checks approval (invariant 1: S21 calls it only after approval), never runs
  a `fix_action`, and passes `attempt` through unchanged (invariant 9).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date
from typing import Any, Final
from uuid import UUID

from mkos_schemas.codes import ERROR_CODE_META, ErrorCode
from mkos_schemas.enums import (
    ItemMarketplaceState,
    Marketplace,
    MarketplaceState,
    RetryClass,
    Severity,
)
from mkos_schemas.errors import ErrorItem, ErrorReport
from mkos_schemas.publish import FieldResult, ItemResult, ListingResult

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
from app.adapters.flipkart import constants as c
from app.adapters.flipkart.client import FlipkartClient, FlipkartResponse, error_rows
from app.adapters.flipkart.mapping import (
    MappedItem,
    build,
    error,
    is_mapped,
    preview,
    reverse_index,
    unsafe_sku,
)

NO_LISTING_ID: Final = UUID(int=0)
MAX_PERFORMANCE_DAYS: Final = 366
NOT_FOUND_CODE: Final = "LISTING_NOT_FOUND"
_ORDER: Final = {"failed": 0, "succeeded": 1, "skipped": 2}
_RETRYABLE: Final = frozenset({RetryClass.AUTO_RETRY, RetryClass.AI_CORRECT})


class _Plan:
    """One SKU of an update: the attributes `names` to send for the paths in `scope`."""

    def __init__(
        self,
        mapped: MappedItem,
        scope: list[str],
        blocked: dict[str, ErrorItem],
        names: list[str],
        sent: list[str],
        errors: Sequence[ErrorItem] = (),
    ) -> None:
        self.mapped = mapped
        self.scope = scope
        self.blocked = blocked
        self.names = names
        self.sent = sent
        self.errors = errors


class _Run:
    def __init__(self) -> None:
        self.bodies: list[bytes] = []
        self.ref: str | None = None
        self.applied = 0
        self.found = 0
        self.missing: list[_Plan] = []


def _not_created(sku: str, paths: Sequence[str]) -> bool:
    """A failed path that means Flipkart holds no listing for this SKU: `listing` (the SKU was
    refused at create) or its own `variants[<sku>]` row."""
    return any(p in ("listing", f"variants[{sku}]") for p in paths)


def _full_plan(
    one: MappedItem, errors: Sequence[ErrorItem], requested: Sequence[str] = ()
) -> _Plan:
    """A SKU Flipkart never created: its whole create payload, every path in scope, plus the
    requested `listing`/`variants[<sku>]` paths, reported with the create's answer."""
    blocked = {e.field: e for e in errors if e.field in one.paths}
    extra = [p for p in requested if p not in one.paths]
    sent = [p for p in one.paths if p in one.submitted and p not in blocked] + extra
    return _Plan(one, sorted({*one.paths, *extra}), blocked, [], sent, errors)


class _Outcome:
    def __init__(self, item: ItemResult, *, applied: bool) -> None:
        self.item = item
        self.applied = applied


def _chunks(items: Sequence[MappedItem]) -> list[list[MappedItem]]:
    return [list(items[i : i + c.CHUNK]) for i in range(0, len(items), c.CHUNK)]


class FlipkartAdapter(MarketplaceAdapter):
    def __init__(
        self,
        client: FlipkartClient,
        error_map: ErrorMapper,
        sink: RawResponseSink | None = None,
    ) -> None:
        self._client = client
        self._map = error_map
        self._sink = sink or NullSink()
        self.marketplace = Marketplace.FLIPKART

    def __repr__(self) -> str:
        return "FlipkartAdapter()"

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---- preview ----

    def preview(self, item: AdapterListing) -> AdapterPreview:
        return preview(item)

    # ---- per-SKU answers -> field results ----

    def _issue_items(
        self, result: Mapping[str, Any], index: Mapping[str, tuple[str, ...]], scope: Iterable[str]
    ) -> tuple[dict[str, ErrorItem], dict[str, ErrorItem], list[ErrorItem]]:
        """(failed path -> error, warned path -> warning, item-level errors)."""
        in_scope = set(scope)
        failed: dict[str, ErrorItem] = {}
        warned: dict[str, ErrorItem] = {}
        loose: list[ErrorItem] = []
        errors = result.get("errors")
        for issue in errors if isinstance(errors, list) else []:
            if not isinstance(issue, dict):
                continue
            severity = str(issue.get("severity", "ERROR")).upper()
            if severity == "INFO":
                continue
            attribute = issue.get("attribute")
            names = [str(attribute)] if attribute else []
            category = issue.get("category")
            found: Sequence[str | None] = sorted(
                {p for n in names for p in index.get(n, ()) if p in in_scope}
            )
            for path in found or [None]:
                one = self._map.to_item(
                    raw_code=str(issue.get("code", "")),
                    raw_message=str(issue.get("description", "")),
                    field=path,
                    marketplace=Marketplace.FLIPKART,
                    categories=[str(category)] if category else [],
                    secrets=self._client.used_secrets(),
                )
                if severity == c.WARNING:
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
        result: Mapping[str, Any] | None,
        mapped: MappedItem,
        index: Mapping[str, tuple[str, ...]],
        scope: Sequence[str],
        sent: Sequence[str],
    ) -> _Outcome:
        unit = mapped.unit
        if result is None:
            result = {
                "status": c.FAILURE,
                "errors": [
                    {
                        "code": "",
                        "description": "Flipkart did not answer for this SKU.",
                        "severity": "ERROR",
                    }
                ],
            }
        failed, warned, loose = self._issue_items(result, index, scope)
        invalid = str(result.get("status", "")).upper() != c.SUCCESS
        fields: list[FieldResult] = []
        for path in scope:
            if path in failed:
                fields.append(FieldResult(field=path, status="failed", error=failed[path]))
            elif path not in sent or invalid:
                fields.append(FieldResult(field=path, status="skipped"))
            else:
                fields.append(FieldResult(field=path, status="succeeded", error=warned.get(path)))
        for one in loose:
            if all(f.field != one.field for f in fields):
                fields.append(FieldResult(field=one.field, status="failed", error=one))
        if invalid and not any(f.status == "failed" for f in fields):
            fallback = (
                loose[0]
                if loose
                else error(
                    "listing",
                    ErrorCode.MARKETPLACE_UNKNOWN_ERROR,
                    "Flipkart refused the listing without naming an attribute.",
                )
            )
            fields.append(FieldResult(field=fallback.field, status="failed", error=fallback))
        listing_id = result.get("listing_id")
        return _Outcome(
            ItemResult(
                product_id=unit.product_id,
                seller_sku=unit.seller_sku,
                external_id=str(listing_id)[:64] if listing_id and not invalid else None,
                marketplace_state=(
                    ItemMarketplaceState.REJECTED if invalid else ItemMarketplaceState.PROCESSING
                ),
                fields=_dedupe(fields),
            ),
            applied=not invalid,
        )

    # ---- results ----

    @staticmethod
    def _failed_item(unit: AdapterItem, paths: Sequence[str], report: ErrorReport) -> ItemResult:
        base = report.errors[0] if report.errors else None
        fields = [
            FieldResult(
                field=p,
                status="failed",
                error=base.model_copy(update={"field": p}) if base else None,
            )
            for p in paths
        ]
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
        # per item, not per aggregated path: a path one child failed may be applied on another
        succeeded = any(f.status == "succeeded" for one in items for f in one.fields)
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
        external = None
        if len(items) == 1 and not item.is_parentless_family:
            external = items[0].external_id
        return ListingResult(
            listing_id=item.listing_id,
            marketplace=Marketplace.FLIPKART,
            external_id=external,
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
        return ListingResult(
            listing_id=item.listing_id,
            marketplace=Marketplace.FLIPKART,
            status="failed",
            idempotency_key=item.idempotency_key,
            fields=_aggregate(items),
            marketplace_state=MarketplaceState.NOT_SUBMITTED,
            items=items,
            error=_report("failed", ErrorCode.PUBLISH_FAILED, errors, item),
            attempt=item.attempt,
        )

    def _store(self, operation: str, chunk: Sequence[MappedItem], raw: bytes) -> None:
        for one in chunk:
            self._sink.put(one.unit.seller_sku, operation, raw)

    # ---- the interface ----

    async def create_listing(self, item: AdapterListing) -> ListingResult:
        mapped, errors = build(item)
        if any(e.severity is Severity.ERROR for e in errors):
            return self._blocked(item, mapped, errors)
        template = item.template.template if item.template is not None else None
        index = reverse_index(template)
        results: list[ItemResult] = []
        bodies: list[bytes] = []
        ref: str | None = None
        applied = 0
        chunks = _chunks(mapped)
        for position, chunk in enumerate(chunks):
            try:
                response = await self._client.create([m.payload for m in chunk])
            except MarketplaceAmbiguous as exc:
                raise MarketplaceAmbiguous(exc.report, completed=results) from None
            except AdapterError as exc:
                if applied == 0:
                    raise type(exc)(exc.report, completed=()) from None
                results += [self._failed_item(m.unit, m.submitted, exc.report) for m in chunk]
                results += [
                    self._skipped_item(m.unit, m.paths)
                    for later in chunks[position + 1 :]
                    for m in later
                ]
                return self._result(
                    item, results, ref=ref, bodies=bodies, transport_error=exc.report
                )
            bodies.append(response.raw)
            self._store("create", chunk, response.raw)
            ref = ref or response.request_id
            answers = _answers(response)
            for one in chunk:
                outcome = self._outcome(
                    answers(one.unit.seller_sku), one, index, one.paths, one.submitted
                )
                results.append(_mark_not_created(outcome))
                applied += int(outcome.applied)
        return self._result(item, results, ref=ref, bodies=bodies)

    async def publish_listing(self, item: AdapterListing) -> ListingResult:
        return await self.create_listing(item)

    async def update_listing(self, item: AdapterListing, *, fields: UpdateFields) -> ListingResult:
        per_sku = fields_by_sku(item, fields)
        mapped, errors = build(item)
        template = item.template.template if item.template is not None else None
        index = reverse_index(template)
        by_sku = {m.unit.seller_sku: m for m in mapped}
        absent = {s for s, paths in per_sku.items() if _not_created(s, paths)}
        for sku, paths in per_sku.items():
            if sku not in absent and [p for p in paths if p not in by_sku[sku].paths]:
                raise ValueError("a field path this item does not carry")
        if not is_mapped(item.template) or any(unsafe_sku(s) for s in per_sku):
            # an unsafe seller SKU is refused as VALUE_NOT_ALLOWED, as create does (invariant 8)
            return self._blocked(item, [by_sku[s] for s in per_sku], errors)
        plans: list[_Plan] = []
        creates: list[_Plan] = []
        results: list[ItemResult] = []
        for one in [m for m in mapped if m.unit.seller_sku in per_sku]:
            if one.unit.seller_sku in absent:
                # Flipkart never created this SKU (refused at create): its full payload goes
                # through the create route in this attempt (the update route answers NOT_FOUND)
                creates.append(_full_plan(one, errors, per_sku[one.unit.seller_sku]))
                continue
            scope = _expand(per_sku[one.unit.seller_sku], index, one.paths)
            blocked = {e.field: e for e in errors if e.field in scope}
            attributes = one.payload["attributes"]
            names = sorted(
                {
                    name
                    for name, feeds in index.items()
                    if name in attributes and any(p in scope and p not in blocked for p in feeds)
                }
            )
            sent = [p for p in scope if p in one.submitted and p not in blocked]
            if not names:
                results.append(_local_item(one.unit, scope, blocked))
                continue
            plans.append(_Plan(one, scope, blocked, names, sent, errors))
        run = _Run()
        stop = await self._send(item, "update", plans, index, results, run, creates)
        if stop is None and run.missing and run.found == 0 and not creates:
            # every SKU of the update is unknown to Flipkart (the adapter contract)
            raise ListingNotFound(run.missing[0].mapped.unit.seller_sku)
        creates += run.missing
        if stop is None:
            stop = await self._send(item, "create", creates, index, results, run, [])
        if stop is not None:
            return stop
        order = {m.unit.seller_sku: n for n, m in enumerate(mapped)}
        results.sort(key=lambda r: order[r.seller_sku])
        return self._result(item, results, ref=run.ref, bodies=run.bodies)

    async def _send(
        self,
        item: AdapterListing,
        operation: str,
        plans: list[_Plan],
        index: Mapping[str, tuple[str, ...]],
        results: list[ItemResult],
        run: _Run,
        creates: list[_Plan],
    ) -> ListingResult | None:
        """Send `plans` through the update or create route in chunks, appending each SKU's
        result. An update answered NOT_FOUND moves the SKU, full payload, to `creates`. Returns
        a finished `partially_published` result when a transport failure follows an applied
        chunk (S20 C2), else None. A SKU to create whose own values `check()` refused is
        reported failed and not sent, as `create_listing` does."""
        if operation == "create":
            for p in [p for p in plans if p.blocked]:
                results.append(_local_item(p.mapped.unit, p.scope, p.blocked))
            plans = [p for p in plans if not p.blocked]
        batches = [plans[i : i + c.CHUNK] for i in range(0, len(plans), c.CHUNK)]
        for position, batch in enumerate(batches):
            if operation == "update":
                entries: list[Any] = [
                    {
                        "sku": p.mapped.unit.seller_sku,
                        "attributes": {n: p.mapped.payload["attributes"][n] for n in p.names},
                    }
                    for p in batch
                ]
            else:
                entries = [p.mapped.payload for p in batch]
            try:
                if operation == "update":
                    response = await self._client.update(entries)
                else:
                    response = await self._client.create(entries)
            except MarketplaceAmbiguous as exc:
                raise MarketplaceAmbiguous(exc.report, completed=results) from None
            except AdapterError as exc:
                if run.applied == 0:
                    raise type(exc)(exc.report, completed=()) from None
                results += [self._failed_item(p.mapped.unit, p.sent, exc.report) for p in batch]
                results += [
                    self._skipped_item(p.mapped.unit, p.scope)
                    for later in batches[position + 1 :]
                    for p in later
                ]
                results += [
                    self._skipped_item(p.mapped.unit, p.scope) for p in [*creates, *run.missing]
                ]
                return self._result(
                    item, results, ref=run.ref, bodies=run.bodies, transport_error=exc.report
                )
            run.bodies.append(response.raw)
            self._store(operation, [p.mapped for p in batch], response.raw)
            run.ref = run.ref or response.request_id
            answers = _answers(response)
            for p in batch:
                answer = answers(p.mapped.unit.seller_sku)
                if operation == "update" and _not_found(answer):
                    run.missing.append(_full_plan(p.mapped, p.errors, p.scope))
                    continue
                run.found += int(operation == "update")
                outcome = self._outcome(answer, p.mapped, index, p.scope, p.sent)
                merged = {f.field: f for f in outcome.item.fields}
                for path, problem in p.blocked.items():
                    merged[path] = FieldResult(field=path, status="failed", error=problem)
                results.append(
                    outcome.item.model_copy(
                        update={"fields": sorted(merged.values(), key=lambda f: f.field)}
                    )
                )
                run.applied += int(outcome.applied)
        return None

    async def get_listing(
        self, seller_sku: str, *, listing_id: UUID | None = None
    ) -> ListingResult:
        response = await self._client.get([seller_sku])
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
                            "Flipkart did not return the listing.",
                        )
                    ],
                    None,
                )
            )
        listings = body.get("listings")
        found = [
            one
            for one in (listings if isinstance(listings, list) else [])
            if isinstance(one, dict) and one.get("sku") == seller_sku
        ]
        if not found:
            raise ListingNotFound(seller_sku)
        entry = found[0]
        index = reverse_index(None)
        attributes = entry.get("attributes") or {}
        paths: set[str] = set()
        for name, values in attributes.items() if isinstance(attributes, dict) else []:
            if name == c.IMAGE_ATTRIBUTE:
                count = len(values) if isinstance(values, list) else 0
                paths |= {f"images[{i}]" for i in range(min(count, c.MAX_IMAGES))}
            else:
                paths |= {p for p in index.get(name, ()) if p != "variants"}
        open_errors = [
            e
            for e in error_rows(entry)
            if isinstance(e, dict) and str(e.get("severity", "")).upper() == "ERROR"
        ]
        if open_errors:
            status, state = "partially_published", MarketplaceState.PARTIALLY_LIVE
        else:
            status = "published"
            live = str(entry.get("status", "")).upper() == c.ACTIVE
            state = MarketplaceState.LIVE if live else MarketplaceState.PROCESSING
        external = entry.get("listing_id")
        return ListingResult(
            listing_id=listing_id or NO_LISTING_ID,
            marketplace=Marketplace.FLIPKART,
            external_id=str(external)[:64] if external else None,
            status=status,
            idempotency_key=f"get:{seller_sku}"[:255],
            fields=[FieldResult(field=p, status="succeeded") for p in sorted(paths)],
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
        raise NotImplementedError("Flipkart performance data is not in v1")


# ---- helpers ----


def _answers(response: FlipkartResponse) -> Callable[[str], Mapping[str, Any] | None]:
    """The per-SKU answer lookup of one create or update response. A non-2xx answer (a 400 for
    the whole request) is every SKU's answer; a SKU missing from the results gets None."""
    body = response.json if isinstance(response.json, dict) else {}
    results = body.get("results")
    if response.status >= 300 or not isinstance(results, list):
        whole: Mapping[str, Any] = {"status": c.FAILURE, "errors": error_rows(body)}
        return lambda sku: whole
    out: dict[str, Mapping[str, Any]] = {}
    for one in results:
        if isinstance(one, dict) and isinstance(one.get("sku"), str):
            out.setdefault(one["sku"], one)
    return out.get


def _not_found(answer: Mapping[str, Any] | None) -> bool:
    if answer is None or str(answer.get("status", "")).upper() == c.SUCCESS:
        return False
    return any(isinstance(e, dict) and e.get("code") == NOT_FOUND_CODE for e in error_rows(answer))


def _mark_not_created(outcome: _Outcome) -> ItemResult:
    """A SKU Flipkart refused at create holds no listing there: it also fails `listing`, so a
    retry of its failed paths sends it through the create route again (S22b F3, F4)."""
    item = outcome.item
    if outcome.applied or any(f.field == "listing" for f in item.fields):
        return item
    first = next((f.error for f in item.fields if f.error is not None), None)
    if first is None:
        return item
    marker = FieldResult(
        field="listing", status="failed", error=first.model_copy(update={"field": "listing"})
    )
    return item.model_copy(update={"fields": sorted([*item.fields, marker], key=lambda f: f.field)})


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
    """`paths` plus every path that feeds the same Flipkart attribute (C7)."""
    out = set(paths)
    for feeds in index.values():
        if out & set(feeds):
            out |= {p for p in feeds if p in carried}
    return sorted(out)


def _local_item(
    unit: AdapterItem, scope: Sequence[str], blocked: Mapping[str, ErrorItem]
) -> ItemResult:
    return ItemResult(
        product_id=unit.product_id,
        seller_sku=unit.seller_sku,
        marketplace_state=ItemMarketplaceState.NOT_SUBMITTED,
        fields=[
            (
                FieldResult(field=p, status="failed", error=blocked[p])
                if p in blocked
                else FieldResult(field=p, status="skipped")
            )
            for p in scope
        ],
    )


def _report(
    status: str,
    code: ErrorCode,
    errors: Sequence[ErrorItem],
    item: AdapterListing | None,
    *,
    retryable: bool | None = None,
) -> ErrorReport:
    if retryable is None:
        retryable = ERROR_CODE_META[code].retry_class in _RETRYABLE
    return ErrorReport.model_validate(
        {
            "status": status,
            "error_code": code,
            "retryable": retryable,
            "errors": list(errors)[:500],
            "listing_id": item.listing_id if item is not None else None,
        }
    )
