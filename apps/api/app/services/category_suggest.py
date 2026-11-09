"""`POST /products/{product_id}/category-suggestion` (SI14, AC4): classify a product into a
suggested category, which the seller confirms. The model never sets `products.category`.

Order (CODE-PLAN T11, the `size_chart_read.request_read` order):
1. A completed replay of the Idempotency-Key answers its stored status and body; another body
   under the key is 409 `IDEMPOTENCY_KEY_REUSED`.
2. The product (404, tenant-scoped; a foreign one answers as a missing one); a variant is 422
   `FACT_LEVEL_INVALID` (the category is the style's). While `CATEGORY_SUGGEST_ENABLED` is off a
   valid ask answers 200 `unavailable`: no job, no admission, no spend, nothing stored.
3. Without a lock: a stored `suggested` result for the current input digest answers 200 with no
   job; a live job answers 202 with it.
4. The SI10 admission, outside any unit of work: AI paused is 503 with the pause's code, a daily
   cap 429 `BUDGET_EXCEEDED`. Nothing is written on a refusal.
5. Under the tenant AI lock and the product row lock step 3 runs again, and only when it finds
   nothing is one `image_compliance` job enqueued (entity `category_suggestion`, entity = the
   product).

The input digest is sha256 over the canonical JSON of the classify text (`classify_text`), the
sorted clean hashes of the photos the classifier would see, and the offered categories, so a title
edit, a new photo or a lamps switch flip asks again.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ImageSlot, JobKind

from app.core.config import Settings, get_settings
from app.core.errors import InputError, NotFoundError
from app.core.logging import get_logger
from app.core.rbac import Principal
from app.db.models.catalog import Product
from app.repositories.idempotency import IdempotencyKeyRepository, Mismatch, Replay
from app.repositories.images import ListingImageRow
from app.repositories.jobs import EnqueueSpec
from app.repositories.uow import TenantUnitOfWork, repositories_on, tenant_uow
from app.services.budget import admit, refuse
from app.services.category_gate import CategoryOffer, category_offers
from app.services.generation_images import pick_context
from app.services.idempotency import idempotent, key_reused, request_hash
from app.workers.budget import CLASSIFY_RESERVATION_USD
from app.workers.policy import CATEGORY_SUGGESTION_ENTITY, lock_for

__all__ = [
    "SuggestInputs",
    "classify_images",
    "classify_text",
    "compute_digest",
    "enqueue_suggestion",
    "request_suggestion",
    "stored_suggestion",
    "suggest_inputs",
]

log = get_logger(__name__)
METHOD: Final = "POST"
LIVE: Final = frozenset({"queued", "running"})
MAX_IMAGES: Final = 6
MAX_ATTRIBUTES: Final = 40
TITLE_CHARS: Final = 500
DESCRIPTION_CHARS: Final = 2000
TEXT_CHARS: Final = 200
_KEY: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


@dataclass(frozen=True, slots=True)
class SuggestInputs:
    """What one classification sends, and its digest."""

    text: dict[str, Any]
    images: list[ListingImageRow]
    offers: list[CategoryOffer]
    digest: str


def _short(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = text.strip()
    return text[:limit] or None


def _attribute_text(value: Any) -> str | None:
    fact = value.get("value") if isinstance(value, Mapping) else value
    if fact is None or isinstance(fact, Mapping):
        return None
    if isinstance(fact, list):
        return _short(", ".join(str(item) for item in fact), TEXT_CHARS)
    return _short(str(fact), TEXT_CHARS)


def classify_text(product: Product) -> dict[str, Any]:
    """The product's own words, as `ProductText`: title, description, brand, the seller's
    product type and the flat attribute values. Never a party, a contact or any fact envelope."""
    attributes: dict[str, str] = {}
    facts = product.attributes if isinstance(product.attributes, Mapping) else {}
    for key in sorted(facts):
        if len(attributes) >= MAX_ATTRIBUTES or not _KEY.fullmatch(key):
            continue
        value = _attribute_text(facts[key])
        if value is not None:
            attributes[key] = value
    return {
        "title": (product.title or "").strip()[:TITLE_CHARS] or "untitled",
        "description": _short(product.description, DESCRIPTION_CHARS),
        "brand": _short(product.brand_name, TEXT_CHARS),
        "seller_category": _short(product.product_type or product.sub_category, TEXT_CHARS),
        "attributes": attributes,
    }


def classify_images(rows: Sequence[ListingImageRow]) -> list[ListingImageRow]:
    """Up to 6 publishable photos with a clean copy, in listing order (no packaging label)."""
    picked = [r for r in pick_context(rows) if r.slot != ImageSlot.PACKAGING_LABEL.value]
    return picked[:MAX_IMAGES]


def compute_digest(
    text: Mapping[str, Any], image_hashes: Sequence[str], allowed: Sequence[str]
) -> str:
    canonical = json.dumps(
        {"text": text, "images": sorted(image_hashes), "categories": list(allowed)},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


async def suggest_inputs(
    uow: TenantUnitOfWork, product: Product, settings: Settings
) -> SuggestInputs:
    text = classify_text(product)
    images = classify_images(await uow.images.listing_set(product.id))
    offers = await category_offers(uow, settings)
    hashes = [row.clean_content_hash for row in images if row.clean_content_hash is not None]
    digest = compute_digest(text, hashes, [offer.category for offer in offers])
    return SuggestInputs(text=text, images=images, offers=offers, digest=digest)


def stored_suggestion(product: Product) -> Mapping[str, Any] | None:
    value = product.category_suggestion
    return value if isinstance(value, Mapping) else None


def _body(product_id: uuid.UUID, status: str, job_id: uuid.UUID | None = None) -> dict[str, Any]:
    return {
        "product_id": str(product_id),
        "status": status,
        "job_id": None if job_id is None else str(job_id),
    }


async def _product(uow: TenantUnitOfWork, product_id: uuid.UUID) -> Product:
    product = await uow.products.get_by_id(product_id)
    if product is None:
        raise NotFoundError("product_id")
    if product.parent_product_id is not None:
        raise InputError(ErrorCode.FACT_LEVEL_INVALID, "product_id")
    return product


async def _answer(
    uow: TenantUnitOfWork, product: Product, digest: str
) -> tuple[int, dict[str, Any]] | None:
    """A stored suggestion for this digest (200) or the live job (202), else None."""
    stored = stored_suggestion(product)
    if (
        stored is not None
        and stored.get("status") == "suggested"
        and stored.get("input_digest") == digest
    ):
        return 200, _body(product.id, "suggested")
    job = await uow.job_runs.latest_for_entity(CATEGORY_SUGGESTION_ENTITY, product.id)
    if job is not None and job.status in LIVE:
        return 202, _body(product.id, "queued", job.id)
    return None


async def enqueue_suggestion(
    uow: TenantUnitOfWork,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    product_id: uuid.UUID,
    digest: str,
) -> uuid.UUID:
    """One classification job for the product (the caller holds the locks and admitted it)."""
    enqueued = await uow.job_runs.enqueue(
        EnqueueSpec(
            kind=JobKind.IMAGE_COMPLIANCE,
            entity_type=CATEGORY_SUGGESTION_ENTITY,
            entity_id=product_id,
            created_by=user_id,
            payload={
                "category_suggestion": {"product_id": str(product_id), "input_digest": digest}
            },
            lock=lock_for(JobKind.IMAGE_COMPLIANCE, CATEGORY_SUGGESTION_ENTITY, tenant_id),
        )
    )
    return enqueued.job.id


async def _peek(
    principal: Principal, key: str, path: str, payload: bytes
) -> tuple[int, dict[str, Any]] | None:
    digest = request_hash(METHOD, path, payload)
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        seen = await IdempotencyKeyRepository(uow.session, principal.tenant_id).peek(
            key, f"{METHOD} {path}", digest, principal.user_id
        )
    if isinstance(seen, Mismatch):
        raise key_reused()
    if isinstance(seen, Replay):
        return seen.status, seen.body
    return None


async def request_suggestion(
    principal: Principal, product_id: uuid.UUID, idem_key: str, path: str
) -> tuple[int, dict[str, Any]]:
    tenant = principal.tenant_id
    settings = get_settings()
    payload = json.dumps({"product_id": str(product_id)}, sort_keys=True).encode()
    replay = await _peek(principal, idem_key, path, payload)
    if replay is not None:
        return replay
    async with tenant_uow(tenant, user_id=principal.user_id) as uow:
        product = await _product(uow, product_id)
        if not settings.category_suggest_enabled:
            log.info("category_suggest.disabled", tenant_id=str(tenant), product_id=str(product_id))
            return 200, _body(product_id, "unavailable")
        inputs = await suggest_inputs(uow, product, settings)
        fast = await _answer(uow, product, inputs.digest)
    if fast is not None:
        return fast
    now = datetime.now(UTC)
    refuse(await admit(tenant, now, amount=CLASSIFY_RESERVATION_USD), now)

    async def handler(session: Any) -> tuple[int, dict[str, Any]]:
        uow = repositories_on(session, tenant)
        await uow.images.lock_tenant_image_jobs()
        locked = await uow.products.get_for_update(product_id)
        if locked is None:
            raise NotFoundError("product_id")
        current = await _product(uow, product_id)
        fresh = await suggest_inputs(uow, current, settings)
        again = await _answer(uow, current, fresh.digest)
        if again is not None:
            return again
        job_id = await enqueue_suggestion(uow, tenant, principal.user_id, product_id, fresh.digest)
        return 202, _body(product_id, "queued", job_id)

    status, body = await idempotent(principal, idem_key, METHOD, path, payload, handler)
    log.info(
        "category_suggest.requested",
        tenant_id=str(tenant),
        product_id=str(product_id),
        job_id=body.get("job_id"),
        status=body.get("status"),
    )
    return status, body
