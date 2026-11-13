"""`POST /products/{product_id}/size-run` (V1.1, client input 8 Nov): make the size variants from
the seller's saved size chart, one size each, only for the sizes the seller ticked.

Nothing is created without this request: the seller saves the size chart first (typed, or a read
of the size-chart photo they confirmed), then picks the sizes and confirms. The model never
creates a variant; the sizes come from the stored seller chart, never from a read suggestion.

`create_size_run(principal, product_id, sizes, idem_key, path)`, idempotent on the key, in one
transaction under the product's row lock, then its listings' locks in id order:

1. The product: missing or another tenant's is 404; a variant is 422 `FACT_LEVEL_INVALID`; no
   category is 422 `VALIDATION_FAILED` at `category` (a variant takes its style's category); a
   style split by colour (`color`, `size_color`) is 422 `VARIATION_INVALID` (its sizes come per
   colourway from the sheet).
2. The sizes: each must be a row of the saved size chart (`size_measurements`; none saved is 422
   `VALIDATION_FAILED` at `size_chart`; only a `seller` chart counts), matched case-insensitively and kept in the chart's order
   and spelling; its key (lower-cased, `SIZE_KEY_PATTERN`, as the import keys it) and its SKU must
   be valid, else 422 at `sizes[i]`. More than `MAX_SIZES` sizes on the style in all is 422 at
   `sizes`.
3. A standalone becomes a size style (`variation_theme='size'`). It must hold nothing a style may
   not own: a barcode (422 `VARIATION_INVALID` at `product_identifier`: a barcode belongs to one
   size), a stock reference (`stock_ref`) or its own size (`attributes.size`). A GTIN exemption
   moves to every new size; a size added later to a style whose sizes are all exempt is exempt.
4. Every listing: one the marketplace has seen is 409 `INVALID_STATUS_TRANSITION` at
   `listing_published`; a generating one 409 `LISTING_BUSY`. Its content was written for the old
   family, so a `validated`, `needs_review` or `approved` listing moves to
   `blocked/validation_errors` (event `revalidate`, the approval voided), as a recategorise does.
5. Each new size is one child: SKU `<style sku>-<SIZE>` (the size label upper-cased, spaces as
   `-`; the family rule the seeds and imports use), the style's title and category, the `size`
   seller fact and one available matrix cell. Sizes the style already has are kept as they are.
   Any SKU already taken in the brand is 409 `SKU_CONFLICT` at `sizes[i]`, nothing created.
6. One `product.size_run_created` audit row (the new SKUs and superseded listings in `reason`).

A repeat with only existing sizes answers 200 with nothing created.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from mkos_schemas.codes import ErrorCode

from app.core.errors import ConflictError, InputError, NotFoundError
from app.core.logging import get_logger
from app.core.rbac import Principal
from app.db.models.catalog import Product
from app.repositories.listings import UserActor
from app.repositories.uow import TenantUnitOfWork, repositories_on
from app.repositories.variants import MatrixCellRepository
from app.services import family_conversion as conversion
from app.services.idempotency import idempotent
from app.services.imports.row_model import SIZE_KEY_PATTERN
from app.services.product_facts import SIZE_CHART_KEY, fact_stamp

__all__ = ["MAX_SIZES", "SKU_MAX", "create_size_run", "size_key", "size_sku"]

log = get_logger(__name__)
METHOD: Final = "POST"
MAX_SIZES: Final = 40  # ck_products_size_run: 1..40 sizes
SKU_MAX: Final = 64
_SKU_OK: Final = re.compile(r"^[^\]\s]{1,64}$")
SPLIT_BY_COLOUR: Final = frozenset({"color", "size_color"})


@dataclass(frozen=True, slots=True)
class _Style:
    """The columns the children copy, read before the style row is written (and expired)."""

    id: uuid.UUID
    title: str
    category: str | None


@dataclass(frozen=True, slots=True)
class _Size:
    label: str  # the chart's spelling (`XL`)
    key: str  # the matrix key (`xl`)
    sku: str


def size_key(label: str) -> str | None:
    """The size's matrix key, as the import keys a size cell: lower-cased, or None when it does
    not fit `SIZE_KEY_PATTERN`."""
    key = " ".join(label.split()).lower()
    return key if SIZE_KEY_PATTERN.fullmatch(key) else None


def size_sku(style_sku: str, label: str) -> str | None:
    """`<style sku>-<SIZE>`: the label upper-cased with each run of spaces as `-`; None when the
    result is not a valid SKU (too long, or a `]`)."""
    token = "-".join(label.upper().split())
    sku = f"{style_sku}-{token}"
    return sku if _SKU_OK.fullmatch(sku) and len(sku) <= SKU_MAX else None


def _chart_sizes(row: Product) -> list[str]:
    attrs = row.attributes if isinstance(row.attributes, Mapping) else {}
    fact = attrs.get(SIZE_CHART_KEY)
    # only a chart the seller saved (typed, or a size-chart read they confirmed) makes sizes
    if not isinstance(fact, Mapping) or fact.get("source") != "seller":
        raise InputError(ErrorCode.VALIDATION_FAILED, "size_chart")
    table = fact.get("normalized")
    rows = table.get("rows") if isinstance(table, Mapping) else None
    if not isinstance(rows, list) or not rows:
        raise InputError(ErrorCode.VALIDATION_FAILED, "size_chart")
    return [r["size"] for r in rows if isinstance(r, Mapping) and isinstance(r.get("size"), str)]


def _picked(row: Product, sizes: Sequence[str]) -> list[_Size]:
    """The asked sizes as chart rows, in the chart's order (the module docstring, step 2)."""
    chart = _chart_sizes(row)
    by_fold = {label.casefold(): label for label in chart}
    asked: set[str] = set()
    for i, size in enumerate(sizes):
        label = by_fold.get(" ".join(size.split()).casefold())
        if label is None or label in asked:
            raise InputError(ErrorCode.VALIDATION_FAILED, f"sizes[{i}]")
        if size_key(label) is None or size_sku(row.sku, label) is None:
            raise InputError(ErrorCode.VALIDATION_FAILED, f"sizes[{i}]")
        asked.add(label)
    picked = []
    for label in chart:
        if label in asked:
            key, sku = size_key(label), size_sku(row.sku, label)
            assert key is not None and sku is not None  # noqa: S101 (checked above)
            picked.append(_Size(label, key, sku))
    return picked


def _check_level(row: Product) -> None:
    if row.parent_product_id is not None:
        raise InputError(ErrorCode.FACT_LEVEL_INVALID, "product_id")
    if row.category is None:
        raise InputError(ErrorCode.VALIDATION_FAILED, "category")
    if row.variation_theme in SPLIT_BY_COLOUR:
        raise InputError(ErrorCode.VARIATION_INVALID, "product_id")
    if row.variation_theme is None:  # a standalone becomes a size style (step 3)
        held = conversion.blocker(row)
        if held is not None:
            raise InputError(ErrorCode.VARIATION_INVALID, held)


def _seller(stamp: str) -> dict[str, object]:
    return {"source": "seller", "captured_via": "api", "updated_at": stamp}


async def _make_children(
    uow: TenantUnitOfWork,
    principal: Principal,
    row: _Style,
    new: Sequence[_Size],
    *,
    exempt: bool,
    stamp: str,
) -> list[dict[str, str]]:
    cells = MatrixCellRepository(uow.session, principal.tenant_id)
    created: list[dict[str, str]] = []
    for size in new:
        provenance: dict[str, Any] = {"title": _seller(stamp), "category": _seller(stamp)}
        child = Product(
            sku=size.sku,
            title=row.title,
            category=row.category,
            parent_product_id=row.id,
            variant_attributes={"size": size.key},
            attributes={"size": {"value": size.label, "normalized": size.key, **_seller(stamp)}},
            provenance=provenance,
            created_by=principal.user_id,
        )
        if exempt:
            child.identifier_status = "exempt"
            provenance["identifier"] = _seller(stamp)
        added = await uow.products.add(child)
        await cells.upsert(row.id, None, size.key, added.id)
        created.append({"id": str(added.id), "sku": size.sku, "size": size.label})
    return created


async def _apply(
    uow: TenantUnitOfWork, principal: Principal, product_id: uuid.UUID, sizes: Sequence[str]
) -> tuple[int, dict[str, Any]]:
    row = await uow.products.get_for_update(product_id)
    if row is None:
        raise NotFoundError("product_id")
    _check_level(row)
    picked = _picked(row, sizes)
    style = _Style(row.id, row.title, row.category)
    stored = list(row.size_run or [])
    new = [s for s in picked if s.key not in stored]
    if not new:
        return 200, _body(row.id, stored, [])
    for size in new:
        if await uow.products.id_of_sku(size.sku) is not None:
            index = next(
                i
                for i, s in enumerate(sizes)
                if " ".join(s.split()).casefold() == size.label.casefold()
            )
            raise ConflictError(ErrorCode.SKU_CONFLICT, f"sizes[{index}]")
    listings = await conversion.listings(uow, row)
    stamp = fact_stamp(datetime.now(UTC))
    converted = row.variation_theme is None
    if converted:  # the update expires `row`: only `style` and `stored` are read after it
        exempt = await conversion.drop_identity(uow, row)
    else:  # a later size joins an exempt family exempt, or the style would read as not exempt
        exempt = (await uow.products.style_exemptions([row.id]))[row.id]
    size_run = [*stored, *(s.key for s in new)]
    if len(size_run) > MAX_SIZES:  # ck_products_size_run: refuse here, never a 500 from the DB
        raise InputError(ErrorCode.VALIDATION_FAILED, "sizes")
    if converted:
        await uow.products.update_one(style.id, variation_theme="size", size_run=size_run)
    else:
        await uow.products.update_one(style.id, size_run=size_run)
    created = await _make_children(uow, principal, style, new, exempt=exempt, stamp=stamp)
    await MatrixCellRepository(uow.session, principal.tenant_id).check_now()
    await conversion.supersede(uow, listings, UserActor(principal.user_id))
    await uow.audit.write(
        tenant_id=principal.tenant_id,
        actor_type="user",
        actor_user_id=principal.user_id,
        action="product.size_run_created",
        entity_type="product",
        entity_id=style.id,
        reason=_reason(created, listings),
    )
    log.info(
        "product.size_run_created",
        product_id=str(style.id),
        created=len(created),
        converted=converted,
        listings=len(listings),
    )
    return 201, _body(style.id, size_run, created)


def _reason(created: Sequence[Mapping[str, str]], listings: Sequence[Any]) -> str:
    """The new SKUs, then the superseded listings (the product audit keeps no body keys)."""
    text = "skus:" + ",".join(c["sku"] for c in created)
    if listings:
        text += ";listings:" + ",".join(str(x.id) for x in listings)
    return text[:500]


def _body(
    product_id: uuid.UUID, size_run: Sequence[str], created: list[dict[str, str]]
) -> dict[str, Any]:
    return {
        "product_id": str(product_id),
        "variation_theme": "size",
        "size_run": list(size_run),
        "created": created,
    }


async def create_size_run(
    principal: Principal,
    product_id: uuid.UUID,
    sizes: Sequence[str],
    idem_key: str,
    path: str,
) -> tuple[int, dict[str, Any]]:
    payload = json.dumps(
        {"product_id": str(product_id), "sizes": list(sizes)}, sort_keys=True
    ).encode()

    async def handler(session: Any) -> tuple[int, dict[str, Any]]:
        uow = repositories_on(session, principal.tenant_id)
        return await _apply(uow, principal, product_id, sizes)

    return await idempotent(principal, idem_key, METHOD, path, payload, handler)
