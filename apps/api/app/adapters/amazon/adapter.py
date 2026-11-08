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
  attribute ERROR issues -> partially live (S20); a summary with an ASIN and none of those ->
  inactive with `not_buyable` (SI9 B3: Amazon has the product page, the offer or stock is missing;
  DISCOVERABLE-only and an empty status alike); a summary without an ASIN and no DISCOVERABLE ->
  inactive; else (no summary yet) processing; 404 -> `ListingNotFound`.
- Reply shape (audit JOBS-3): a 200 reply whose structure cannot be read (not an object, `status`
  not a string, `issues`/`summaries`/`summaries[].status` not lists, a summary not an object,
  `attributes` not an object) is `MarketplaceAmbiguous` after a write (read before any resend)
  and `MarketplaceUnavailable` on a GET, never a raw TypeError/KeyError. An issue whose
  `attributeNames` or `categories` is not a list names no attribute.
- The adapter never checks approval (invariant 1: S21 calls it only after approval), never runs
  a `fix_action`, and passes `attempt` through unchanged (invariant 9).
- `preview_remote` (SI11): Amazon's dry run of what a create would send (`put_preview`, one per
  item, parent first), with no images (CP-D23: the caller passes no `image_refs`, and an issue on
  an image attribute is dropped). A local ERROR or an unmapped category sends nothing (`failed`).
  VALID everywhere -> `clean`; INVALID -> `issues`, mapped and scrubbed as a write's issues; a
  reply with no status, an unreadable body or any other status -> `failed` with `applied` True
  and one ERROR log `preview.applied` (Amazon may have stored it; the caller opens the account
  breaker). A transport failure propagates (the job's retry). Nothing of the reply is kept.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from typing import Any, Final
from uuid import UUID

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ItemMarketplaceState, Marketplace, MarketplaceState, Severity
from mkos_schemas.errors import MAX_ERROR_ITEMS, ErrorItem, ErrorReport
from mkos_schemas.publish import FieldResult, ItemResult, ListingResult

from app.adapters.amazon import constants as c
from app.adapters.amazon import precheck
from app.adapters.amazon.client import SpApiClient, SpApiResponse, failure
from app.adapters.amazon.mapping import (
    DEFAULT_MARKETPLACE_ID,
    IMAGE_NAMES,
    MappedItem,
    build,
    error,
    fact_path,
    is_mapped,
    item_index,
    preview,
    reverse_index,
)
from app.adapters.amazon.precheck import PrecheckResult
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
    RemotePreview,
    RemoteStatus,
    UpdateFields,
    fields_by_sku,
    issue_text,
    party_secrets,
)
from app.adapters.error_map import ErrorMapper, ListingRead, fold_read_issue, read_issue
from app.core.logging import get_logger

log = get_logger(__name__)
PRODUCT_TYPE_READ: Final = re.compile(r"[A-Z0-9_]{1,64}", re.ASCII)

NO_LISTING_ID: Final = UUID(int=0)
PREVIEW_VERDICTS: Final = frozenset({"VALID", "INVALID"})
IMAGE_ATTRIBUTES: Final = frozenset({c.MAIN_IMAGE, *c.OTHER_IMAGES})
MAX_PERFORMANCE_DAYS: Final = 366
_ORDER: Final = {"failed": 0, "succeeded": 1, "skipped": 2}


class _UnreadableReplyError(Exception):
    """A 200 reply whose structure the adapter cannot read (audit JOBS-3)."""


def _names(value: Any) -> list[str]:
    """An issue's `attributeNames`/`categories`: a list of names, or none at all."""
    return [str(n) for n in value] if isinstance(value, list) else []


def _write_reply(response: SpApiResponse) -> dict[str, Any]:
    """The body of a PUT/PATCH reply; a 200 whose structure cannot be read is `_UnreadableReplyError`."""
    body = response.json
    if not isinstance(body, dict):
        if response.status == 200:
            raise _UnreadableReplyError
        return {}
    if response.status == 200:
        status = body.get("status")
        if status is not None and not isinstance(status, str):
            raise _UnreadableReplyError
        for key in ("issues", "errors"):
            if body.get(key) is not None and not isinstance(body[key], list):
                raise _UnreadableReplyError
    return body


def _unconfirmed(results: Sequence[ItemResult]) -> MarketplaceAmbiguous:
    return MarketplaceAmbiguous(
        failure(
            ErrorCode.MARKETPLACE_UNCONFIRMED, "Amazon answered with a reply it could not read."
        ),
        completed=results,
    )


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

    async def preview_remote(self, item: AdapterListing) -> RemotePreview:
        """SI11: Amazon's VALIDATION_PREVIEW of every item (module docstring)."""
        # The stored-definition check is the local pre-approval check's job (SI9); this asks
        # Amazon itself. A live listing's next send is an update: no barcode refusal (CP-D14).
        mapped, errors = build(
            item,
            marketplace_id=self._marketplace_id,
            schema=False,
            require_identifier=not item.live,
        )
        local = tuple(e for e in errors if e.severity is Severity.ERROR)
        if local:
            return RemotePreview(status="failed", errors=local[:MAX_ERROR_ITEMS])
        template = item.template.template if item.template is not None else None
        index = reverse_index(template)
        parties = party_secrets(item)
        found: list[ErrorItem] = []
        per_sku: dict[str, str] = {}
        scope: set[str] = set()
        for position, one in enumerate(mapped):
            paths = tuple(p for p in one.paths if not p.startswith("images"))
            scope.update(paths)
            response = await self._client.put_preview(one.unit.seller_sku, one.payload)
            if response.status != 200:
                per_sku[one.unit.seller_sku] = str(response.status)
                return RemotePreview(
                    status="failed",
                    errors=(_preview_refused(response.status),),
                    per_sku=per_sku,
                    scope=tuple(sorted(scope)),
                )
            try:
                body = _write_reply(response)
            except _UnreadableReplyError:
                body = {}
            verdict = body.get("status")
            if not isinstance(verdict, str) or verdict.upper() not in PREVIEW_VERDICTS:
                log.error("preview.applied", listing_id=str(item.listing_id), sku_index=position)
                per_sku[one.unit.seller_sku] = "unexpected"
                return RemotePreview(
                    status="failed",
                    applied=True,
                    errors=(_preview_unread(),),
                    per_sku=per_sku,
                    scope=tuple(sorted(scope)),
                )
            per_sku[one.unit.seller_sku] = verdict.upper()
            kept = _without_images(response, body)
            # this session's tokens are known only once a call has gone out
            secrets = (*parties, *self._client.used_secrets())
            failed, _, loose = self._issue_items(
                kept, one, index, paths, dict.fromkeys(paths), secrets
            )
            found += [failed[p] for p in sorted(failed)] + loose
        status: RemoteStatus = "issues" if found or "INVALID" in per_sku.values() else "clean"
        if status == "issues" and not found:
            found.append(
                error(
                    "listing",
                    ErrorCode.MARKETPLACE_UNKNOWN_ERROR,
                    "Amazon's check refused the item without naming an attribute.",
                )
            )
        return RemotePreview(
            status=status,
            errors=tuple(found[:MAX_ERROR_ITEMS]),
            per_sku=per_sku,
            scope=tuple(sorted(scope)),
        )

    # ---- issues -> field results ----

    def _issue_items(
        self,
        response: SpApiResponse,
        mapped: MappedItem,
        index: Mapping[str, tuple[str, ...]],
        scope: Iterable[str],
        values: Mapping[str, str | None],
        secrets: Sequence[str] = (),
    ) -> tuple[dict[str, ErrorItem], dict[str, ErrorItem], list[ErrorItem]]:
        """(failed path -> error, warned path -> warning, item-level errors). `secrets` (SI2:
        the manufacturer strings, `party_secrets`) are scrubbed from every issue text."""
        body = _write_reply(response)
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
            names = _names(issue.get("attributeNames"))
            categories = _names(issue.get("categories"))
            found: Sequence[str | None] = sorted(
                {p for n in names for p in index.get(n, ()) if p in in_scope}
            )
            for path in found or [None]:
                one = self._map.to_item(
                    raw_code=str(issue.get("code", "")),
                    raw_message=issue_text(str(issue.get("message", "")), names, index, secrets),
                    field=path,
                    value=values.get(path) if path else None,
                    categories=categories,
                    secrets=secrets,
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
        secrets: Sequence[str] = (),
    ) -> _Outcome:
        unit = mapped.unit
        values: dict[str, str | None] = dict.fromkeys(scope)  # never a party value (SI2)
        failed, warned, loose = self._issue_items(response, mapped, index, scope, values, secrets)
        body = _write_reply(response)
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
            try:
                outcome = self._outcome(
                    response,
                    one,
                    item_index(index, one),
                    one.paths,
                    one.submitted,
                    party_secrets(item),
                )
            except _UnreadableReplyError:
                raise _unconfirmed(results) from None
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
        # DW2 D5/AC9: schema errors show in preview and the snapshot, never block a PATCH.
        # SI5 CP-D14: a live item is never refused for a missing barcode (a create is).
        mapped, errors = build(
            item, marketplace_id=self._marketplace_id, schema=False, require_identifier=False
        )
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
            own = item_index(index, one)  # SI8 CP-D4: this item's own image slots
            scope = _expand(per_sku[one.unit.seller_sku], own, one.paths)
            blocked = {e.field: e for e in errors if e.field in scope}
            # SI11 (T10): a repaired attribute rides with its field path; a dropped one
            # (`drop_locked`) is simply not in the payload, so no `delete` op is sent
            repaired_ids = {rid for rid, _path, _from, _to in one.repaired}
            repaired = {
                r.attribute
                for r in item.repairs
                if r.id in repaired_ids and r.field_path in scope and r.value is not None
            }
            names = sorted(
                {
                    name
                    for name, feeds in own.items()
                    if name in one.payload["attributes"]
                    and any(p in scope and p not in blocked for p in feeds)
                }
                | (repaired & set(one.payload["attributes"]))
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
                    self._skipped_item(
                        m.unit, _expand(per_sku[m.unit.seller_sku], item_index(index, m), m.paths)
                    )
                    for m in ordered[position + 1 :]
                ]
                return self._result(
                    item, results, ref=ref, bodies=bodies, transport_error=exc.report
                )
            if response.status == 404:
                raise ListingNotFound(one.unit.seller_sku)
            bodies.append(response.raw)
            self._sink.put(one.unit.seller_sku, "patch", response.raw)
            try:
                outcome = self._outcome(response, one, own, scope, sent, party_secrets(item))
            except _UnreadableReplyError:
                raise _unconfirmed(results) from None
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

    async def precheck_catalog(self, item: AdapterListing, *, restrictions: bool) -> PrecheckResult:
        """SI13: the read-only catalog and brand-restriction pre-check (`precheck` module). One
        search per 20 barcodes in SKU order; restrictions once per distinct matched or
        other-brand ASIN when `restrictions`. A failed call puts its SKUs in `failed` and the
        check goes on; a partial search (a `nextToken` left) puts the chunk's unflagged SKUs in
        `failed`. It never raises an adapter error and never writes."""
        units = sorted(
            (u for u in item.units() if precheck.barcode_of(item, u) is not None),
            key=lambda u: u.seller_sku,
        )
        parties = party_secrets(item)
        items: list[ErrorItem] = []
        matched: list[tuple[str, str]] = []
        failed: list[str] = []
        asin_skus: dict[str, list[str]] = {}
        size = c.CATALOG_MAX_IDENTIFIERS
        for start in range(0, len(units), size):
            chunk = units[start : start + size]
            kinds = {precheck.identifier_type_of(item, u) for u in chunk}
            id_type = kinds.pop() if len(kinds) == 1 else "EAN"
            barcodes = list(dict.fromkeys(str(precheck.barcode_of(item, u)) for u in chunk))
            try:
                response = await self._client.search_catalog(barcodes, id_type)
                body = response.json if isinstance(response.json, dict) else {}
                found, same, others = precheck.from_catalog(item, chunk, body.get("items"))
            except (AdapterError, ValueError):
                failed += [u.seller_sku for u in chunk]
                continue
            pagination = body.get("pagination")
            if isinstance(pagination, dict) and pagination.get("nextToken"):
                # a partial search: an unflagged unit may match on a page never read, so it is
                # not checked and stays out of `scope` (its open row is kept, never closed)
                flagged = {i.field for i in found}
                failed += [u.seller_sku for u in chunk if fact_path(u, "identifier") not in flagged]
            items += found
            matched += same
            for sku, asin in same:
                asin_skus.setdefault(asin, []).append(sku)
            for asin in others:
                asin_skus.setdefault(asin, [])
        if restrictions:
            for asin in sorted(asin_skus):
                try:
                    response = await self._client.get_restrictions(asin)
                except (AdapterError, ValueError):
                    failed += asin_skus[asin]
                    continue
                secrets = (*parties, *self._client.used_secrets())
                items += precheck.from_restrictions(response.json, secrets)
        lost = set(failed)
        return precheck.PrecheckResult(
            items=tuple(items[:MAX_ERROR_ITEMS]),
            matched=tuple(matched),
            failed=tuple(dict.fromkeys(failed)),
            checked=len(units),
            scope=tuple(
                sorted(
                    {fact_path(u, "identifier") for u in units if u.seller_sku not in lost}
                    | ({"listing"} if restrictions and not lost else set())
                )
            ),
        )

    async def get_listing(
        self, seller_sku: str, *, listing_id: UUID | None = None
    ) -> ListingResult:
        response = await self._client.get(seller_sku)
        if response.status == 404:
            raise ListingNotFound(seller_sku)
        if response.status != 200:
            raise _unavailable("Amazon did not return the listing.")
        try:
            summaries, summary, states, attributes, issues = _read_listing(response.json)
        except _UnreadableReplyError:
            raise _unavailable("Amazon returned a listing it could not read.") from None
        index = _read_index(attributes)
        paths = sorted({p for name in attributes for p in index.get(name, ()) if p != "variants"})
        open_errors = [
            i
            for i in issues
            if isinstance(i, dict) and str(i.get("severity", "")).upper() == "ERROR"
        ]
        # S21 CP6: BUYABLE -> live; an ERROR issue and no BUYABLE -> rejected; neither BUYABLE nor
        # DISCOVERABLE -> inactive (e.g. search suppression); else processing ("Submitted")
        # an ERROR issue naming attributes is a partial item (S20 contract: a partial create reads
        # back partially live); one naming none refuses the item as a whole
        item_errors = [i for i in open_errors if not _names(i.get("attributeNames"))]
        asin = summary.get("asin")
        status = "published"
        not_buyable = False
        if c.BUYABLE in states:
            state = MarketplaceState.LIVE
        elif item_errors:
            status, state = "failed", MarketplaceState.REJECTED
        elif open_errors:
            status, state = "partially_published", MarketplaceState.PARTIALLY_LIVE
        elif asin:
            # SI9 B3: the product page exists, the offer or stock does not
            state, not_buyable = MarketplaceState.INACTIVE, True
        elif summaries and c.DISCOVERABLE not in states:
            state = MarketplaceState.INACTIVE
        else:
            state = MarketplaceState.PROCESSING
        read_fields, read_errors = self._read_fields(paths, issues, index)
        return ListingRead(
            listing_id=listing_id or NO_LISTING_ID,
            marketplace=Marketplace.AMAZON,
            external_id=str(asin)[:64] if asin else None,
            status=status,
            idempotency_key=f"get:{seller_sku}"[:255],
            fields=read_fields,
            errors=read_errors,
            marketplace_state=state,
            raw_response_digest=hashlib.sha256(response.raw).hexdigest(),
            not_buyable=not_buyable,
            product_type=_read_product_type(summary),
        )

    def _read_fields(
        self, paths: Sequence[str], issues: Sequence[Any], index: Mapping[str, tuple[str, ...]]
    ) -> tuple[list[FieldResult], list[ErrorItem]]:
        """SI4 A16 (AC13): the read's ERROR and WARNING issues as field results (one per path)
        and as items (one per issue, read order), mapped through
        this adapter's error map like a write's (`_issue_items`). A named path fails on that
        path (a warning keeps it `succeeded` with the item); an item-level one is `listing`. The
        raw text is scrubbed of this session's tokens; no value is read back."""
        fields = {p: FieldResult(field=p, status="succeeded") for p in paths}
        errors: list[ErrorItem] = []  # every issue, in read order (round 3 F2)
        secrets = self._client.used_secrets()
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            severity = str(issue.get("severity", "")).upper()
            if severity not in ("ERROR", "WARNING"):
                continue
            names = _names(issue.get("attributeNames"))
            found: Sequence[str | None] = sorted(
                {p for n in names for p in index.get(n, ()) if p != "variants"}
            )
            for path in found or [None]:
                one = self._map.to_item(
                    raw_code=str(issue.get("code", "")),
                    raw_message=str(issue.get("message", "")),
                    field=path,
                    marketplace=Marketplace.AMAZON,
                    categories=_names(issue.get("categories")),
                    secrets=secrets,
                )
                one = read_issue(one, warning=severity == "WARNING")
                errors.append(one)
                fields = fold_read_issue(fields, one, warning=severity == "WARNING")
        return sorted(fields.values(), key=lambda f: f.field), errors[:MAX_ERROR_ITEMS]

    async def get_performance(
        self, listing_id: UUID, from_date: date, to_date: date
    ) -> list[PerformanceDay]:
        if to_date < from_date:
            raise ValueError("to_date is before from_date")
        if (to_date - from_date).days + 1 > MAX_PERFORMANCE_DAYS:
            raise ValueError(f"at most {MAX_PERFORMANCE_DAYS} days per call")
        raise NotImplementedError("Amazon performance data lands with S23")


# ---- helpers ----


def _read_product_type(summary: Mapping[str, Any]) -> str | None:
    """SI13: the summary's `productType`, only when it is a plain product-type name."""
    value = summary.get("productType")
    return value if isinstance(value, str) and PRODUCT_TYPE_READ.fullmatch(value) else None


def _without_images(response: SpApiResponse, body: Mapping[str, Any]) -> SpApiResponse:
    """The preview reply with every issue on an image attribute removed (CP-D23: a preview sends
    no image, so Amazon's "missing image" is the gap the preview made itself)."""
    issues = body.get("issues") or body.get("errors") or []
    kept = [
        i
        for i in (issues if isinstance(issues, list) else [])
        if not (isinstance(i, dict) and set(_names(i.get("attributeNames"))) & IMAGE_ATTRIBUTES)
    ]
    return SpApiResponse(
        status=response.status,
        json={**body, "issues": kept, "errors": None},
        headers=response.headers,
        raw=b"",
    )


def _preview_refused(status: int) -> ErrorItem:
    return error(
        "listing",
        ErrorCode.MARKETPLACE_UNAVAILABLE,
        f"Amazon's check answered {status}; nothing was judged.",
    )


def _preview_unread() -> ErrorItem:
    return error(
        "listing",
        ErrorCode.MARKETPLACE_UNCONFIRMED,
        "Amazon's check answered in a way Kuinn could not read.",
    )


def _unavailable(message: str) -> MarketplaceUnavailable:
    return MarketplaceUnavailable(
        _report(
            "failed",
            ErrorCode.MARKETPLACE_UNAVAILABLE,
            [error("listing", ErrorCode.MARKETPLACE_UNAVAILABLE, message)],
            None,
        )
    )


def _is_child(attributes: Mapping[str, Any]) -> bool:
    """The read item is a variation child: its `parentage_level` says `child`, or it names a
    parent SKU (a child always sends both; mapping `family_payloads`)."""
    if c.RELATIONSHIP_ATTRIBUTE in attributes:
        return True
    levels = attributes.get(c.PARENTAGE_ATTRIBUTE)
    return isinstance(levels, list) and any(
        isinstance(one, dict) and str(one.get("value", "")).casefold() == "child" for one in levels
    )


def _theme_has_colour(attributes: Mapping[str, Any]) -> bool:
    """True unless the read's `variation_theme` names a theme with no COLOR axis."""
    themes = attributes.get(c.THEME_ATTRIBUTE)
    names = [
        str(one.get("name", "")).upper()
        for one in (themes if isinstance(themes, list) else [])
        if isinstance(one, dict)
    ]
    return not names or any("COLOR" in name.split("/") for name in names)


def _read_index(attributes: Mapping[str, Any]) -> Mapping[str, tuple[str, ...]]:
    """The read-back index. `reverse_index(None)` reads image slot k as `images[k]`, which holds
    for a standalone (every image, version order). A colour child's slots carry its own
    colourway's images first (SI8 T4, CP-D4), and a read has no version to rebuild them from, so
    a child's read drops the image rows: an image issue fails `listing` and no image path reads
    as succeeded, never a wrong `images[k]` (SI8 fix round 1, R1-1). A child whose theme has no
    COLOR axis (e.g. `SIZE`) sends every image in version order, so it keeps the image rows; a
    child with no readable theme drops them."""
    index = reverse_index(None)
    if not _is_child(attributes) or not _theme_has_colour(attributes):
        return index
    return {name: paths for name, paths in index.items() if name not in IMAGE_NAMES}


def _read_listing(
    raw: Any,
) -> tuple[list[Any], dict[str, Any], list[str], dict[str, Any], list[Any]]:
    """(summaries, first summary, its states, attributes, issues) of a 200 GET reply;
    `_UnreadableReplyError` when its structure is not the one SP-API documents (audit JOBS-3)."""
    if not isinstance(raw, dict):
        raise _UnreadableReplyError
    summaries = raw.get("summaries")
    if summaries is None:
        summaries = []
    if not isinstance(summaries, list) or not all(isinstance(s, dict) for s in summaries):
        raise _UnreadableReplyError
    summary: dict[str, Any] = summaries[0] if summaries else {}
    status = summary.get("status")
    if status is None:
        status = []
    if not isinstance(status, list) or not all(isinstance(s, str) for s in status):
        raise _UnreadableReplyError
    attributes = raw.get("attributes")
    if attributes is None:
        attributes = {}
    issues = raw.get("issues")
    if issues is None:
        issues = []
    if not isinstance(attributes, dict) or not isinstance(issues, list):
        raise _UnreadableReplyError
    return summaries, summary, [s.upper() for s in status], attributes, issues


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
