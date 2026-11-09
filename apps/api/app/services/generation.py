"""Generation requests (S18 T9, T10; DATABASE.md:1379-1402; CANON.md:152).

`request_generation(uow, principal, http_key, items)` takes 1..200 items (D1: a single action is a
batch of one), each in its own SAVEPOINT, in order:

0. (CP12, before any write) instructions holding JSON with a secret-shaped key → 422
   `VALIDATION_FAILED` on `user_instructions`; the value is never echoed or logged;
1. the product: missing or soft-deleted → 404; a variant child → 422 (`product_id`);
2. the category (the item's, else the product's) needs a current template for the marketplace →
   else 422 (`category`);
3. the listing of `(product, marketplace)` under `FOR UPDATE`, created when missing; another
   category than the listing's → 422 (`category`);
4. the status: `generating` → 409 `LISTING_BUSY`; paused/archived → 409
   `LISTING_PAUSED|LISTING_ARCHIVED`; `created` → generate; any other → the regenerate path when
   `force_regenerate`, else 409 `INVALID_STATUS_TRANSITION`;
5. the `listing_generate` job (key `job_idempotency_key(http_key, kind, listing_id)`; payload
   `{"user_instructions"}` only, D3), deferred in the same transaction;
6. the move `created → generating` (event `generate`, the user as actor).

`accept_generate` and `accept_regenerate` run, after the budget pre-check, the per-tenant live
cap: at most `GENERATION_LIVE_JOBS_PER_TENANT` `listing_generate` jobs `queued|running`, counted
under the tenant's enqueue lock, else 429 `RATE_LIMITED` (`enforce_live_generate_cap`). Claiming
stays FIFO on the shared `default` queue; the cap bounds what one brand can put ahead of others.

`request_regeneration(...)` (T10): under the listing lock, `generating` → 409 `LISTING_BUSY`,
paused/archived → 409, `created` → 409 `INVALID_STATUS_TRANSITION`; otherwise the job, then the
`regenerate_requested` decision on the current version (NULL when there is none) naming the job,
then the move to `generating` (event `regenerate`) with the approval cleared (it voids it). A
reviewer may regenerate but not generate fresh (the route's action decides).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from mkos_schemas.base import find_seam_secret_key
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import JobKind, JobState, Marketplace
from mkos_schemas.jobs import GenerateListingRequest

from app.core.config import get_settings
from app.core.errors import ConflictError, DomainError, InputError, LimitError, NotFoundError
from app.core.logging import get_logger
from app.core.rbac import Principal
from app.core.text import has_control_char
from app.repositories.jobs import EnqueueSpec
from app.repositories.listings import UserActor
from app.repositories.uow import TenantUnitOfWork, repositories_on, tenant_uow

log = get_logger(__name__)

MAX_ITEMS: Final = 200
GENERATING: Final = "generating"
CREATED: Final = "created"
PAYLOAD_KEY: Final = "user_instructions"


@dataclass(frozen=True, slots=True)
class GenerateItem:
    product_id: uuid.UUID
    marketplace: Marketplace
    category: str | None = None
    user_instructions: str | None = None
    force_regenerate: bool = False


@dataclass(frozen=True, slots=True)
class ItemAccepted:
    job_id: uuid.UUID
    listing_id: uuid.UUID
    status: JobState = JobState.QUEUED


@dataclass(frozen=True, slots=True)
class ItemRefused:
    error: DomainError


ItemResult = ItemAccepted | ItemRefused


def check_instructions(text: str | None, field: str) -> None:
    """CP12: instructions are data, but JSON in them with a secret-shaped key is refused at the
    API (before any write) instead of being stored for good and refused at claim."""
    if text is not None and (find_seam_secret_key(text) is not None or has_control_char(text)):
        raise InputError(ErrorCode.VALIDATION_FAILED, field)


def _status_gate(listing: Any) -> None:
    if listing.paused_at is not None:
        raise ConflictError(ErrorCode.LISTING_PAUSED, "paused")
    if listing.archived_at is not None:
        raise ConflictError(ErrorCode.LISTING_ARCHIVED, "archived")
    if listing.status == GENERATING:
        raise ConflictError(ErrorCode.LISTING_BUSY, "listings")


async def enqueue_generate(
    uow: TenantUnitOfWork,
    principal: Principal,
    http_key: str,
    listing_id: uuid.UUID,
    instructions: str | None,
) -> uuid.UUID:
    enqueued = await uow.job_runs.enqueue(
        EnqueueSpec(
            kind=JobKind.LISTING_GENERATE,
            entity_type="listing",
            entity_id=listing_id,
            listing_id=listing_id,
            created_by=principal.user_id,
            http_key=http_key,
            payload={PAYLOAD_KEY: instructions},
        )
    )
    if not enqueued.created:  # the derived key already names a job: a live one is busy
        raise ConflictError(ErrorCode.LISTING_BUSY, "job_runs")
    return enqueued.job.id


async def _regenerate_locked(
    uow: TenantUnitOfWork,
    principal: Principal,
    http_key: str,
    listing: Any,
    instructions: str | None,
) -> ItemAccepted:
    _status_gate(listing)
    if listing.status == CREATED:
        raise ConflictError(ErrorCode.INVALID_STATUS_TRANSITION, "listings")
    job_id = await enqueue_generate(uow, principal, http_key, listing.id, instructions)
    await uow.review_decisions.insert_regenerate_requested(
        listing.id, listing.current_version_id, instructions, job_id
    )
    from_status = listing.status
    await uow.listings.move(
        listing,
        to=GENERATING,
        event="regenerate",
        actor=UserActor(principal.user_id),
        clear_approval=True,
    )
    await uow.audit.write(
        tenant_id=principal.tenant_id,
        actor_type="user",
        actor_user_id=principal.user_id,
        action="listing.regenerate_requested",
        entity_type="listing",
        entity_id=listing.id,
        # `audit()` keeps the listing's allow-listed keys only (0004); the job is in job_runs.
        before=json.dumps({"status": from_status}),
        after=json.dumps({"status": GENERATING}),
    )
    log.info(
        "generation.requested",
        listing_id=str(listing.id),
        job_id=str(job_id),
        listing_event="regenerate",
    )
    return ItemAccepted(job_id=job_id, listing_id=listing.id)


async def _generate_one(
    uow: TenantUnitOfWork, principal: Principal, http_key: str, item: GenerateItem
) -> ItemAccepted:
    marketplace = Marketplace(item.marketplace).value
    product = await uow.products.get_by_id(item.product_id)
    if product is None:
        raise NotFoundError("product_id")
    if product.parent_product_id is not None:
        raise InputError(ErrorCode.VALIDATION_FAILED, "product_id")
    category = item.category or product.category
    if category is None:  # no category yet (0027, D3): the seller chooses one first
        raise InputError(ErrorCode.VALIDATION_FAILED, "category")
    if await uow.templates.current(marketplace, category) is None:
        raise InputError(ErrorCode.VALIDATION_FAILED, "category")
    listing = await uow.listings.get_by_pair_for_update(product.id, marketplace)
    if listing is None:
        listing = await uow.listings.create(product.id, marketplace, category)
    if listing.category != category:
        raise InputError(ErrorCode.VALIDATION_FAILED, "category")
    _status_gate(listing)
    if listing.status != CREATED:
        if not item.force_regenerate:
            raise ConflictError(ErrorCode.INVALID_STATUS_TRANSITION, "listings")
        return await _regenerate_locked(uow, principal, http_key, listing, item.user_instructions)
    job_id = await enqueue_generate(uow, principal, http_key, listing.id, item.user_instructions)
    await uow.listings.move(
        listing, to=GENERATING, event="generate", actor=UserActor(principal.user_id)
    )
    log.info(
        "generation.requested",
        listing_id=str(listing.id),
        job_id=str(job_id),
        listing_event="generate",
    )
    return ItemAccepted(job_id=job_id, listing_id=listing.id)


async def request_generation(
    uow: TenantUnitOfWork,
    principal: Principal,
    http_key: str,
    items: Sequence[GenerateItem],
) -> list[ItemResult]:
    """Each item in its own SAVEPOINT (module docstring). A refused item is an `ItemRefused`;
    the caller of a single item raises its error."""
    if not 1 <= len(items) <= MAX_ITEMS:
        raise InputError(ErrorCode.VALIDATION_FAILED, "items")
    for item in items:
        check_instructions(item.user_instructions, PAYLOAD_KEY)
    results: list[ItemResult] = []
    for item in items:
        try:
            async with uow.session.begin_nested():
                results.append(await _generate_one(uow, principal, http_key, item))
        except DomainError as exc:
            results.append(ItemRefused(exc))
    return results


async def request_regeneration(
    uow: TenantUnitOfWork,
    principal: Principal,
    http_key: str,
    listing_id: uuid.UUID,
    instructions: str | None,
) -> ItemAccepted:
    check_instructions(instructions, "instructions")
    listing = await uow.listings.get_for_update(listing_id)
    if listing is None:
        raise NotFoundError("listing_id")
    return await _regenerate_locked(uow, principal, http_key, listing, instructions)


async def enforce_live_generate_cap(uow: TenantUnitOfWork) -> None:
    """At most `GENERATION_LIVE_JOBS_PER_TENANT` `listing_generate` jobs `queued|running` per
    tenant, counted under the tenant's enqueue lock: past it, 429 `RATE_LIMITED`. One brand
    cannot fill the shared `default` queue (4 slots, FIFO) and starve every other brand."""
    await uow.job_runs.lock_generate_enqueue()
    if (
        await uow.job_runs.count_live_generations()
        >= get_settings().generation_live_jobs_per_tenant
    ):
        raise LimitError("live generations per tenant")


async def accept_generate(
    principal: Principal, key: str, path: str, body: GenerateListingRequest
) -> dict[str, Any]:
    """Idempotent 202. A stored replay skips the budget pre-check (CP6). The admission read runs
    first, holding no connection; the handler raises its refusal."""
    from app.services.budget import admit, refuse
    from app.services.idempotency import idempotent

    payload = body.model_dump_json().encode()
    now = datetime.now(UTC)
    refusal = await admit(principal.tenant_id, now)

    async def handler(session: Any) -> tuple[int, dict[str, Any]]:
        uow = repositories_on(session, principal.tenant_id)
        refuse(refusal, now)
        await enforce_live_generate_cap(uow)
        [result] = await request_generation(
            uow,
            principal,
            key,
            [
                GenerateItem(
                    product_id=body.product_id,
                    marketplace=body.marketplace,
                    category=body.category,
                    user_instructions=body.user_instructions,
                    force_regenerate=body.force_regenerate,
                )
            ],
        )
        if isinstance(result, ItemRefused):
            raise result.error
        return 202, accepted_body(result)

    _status, stored = await idempotent(principal, key, "POST", path, payload, handler)
    return stored


async def accept_regenerate(
    principal: Principal,
    key: str,
    path: str,
    listing_id: uuid.UUID,
    instructions: str | None,
) -> dict[str, Any]:
    from app.services.budget import admit, refuse
    from app.services.idempotency import idempotent

    payload = json.dumps({"instructions": instructions}, sort_keys=True).encode()
    now = datetime.now(UTC)
    refusal = await admit(principal.tenant_id, now)  # before any connection is held

    async def handler(session: Any) -> tuple[int, dict[str, Any]]:
        uow = repositories_on(session, principal.tenant_id)
        refuse(refusal, now)
        await enforce_live_generate_cap(uow)
        accepted = await request_regeneration(uow, principal, key, listing_id, instructions)
        return 202, accepted_body(accepted)

    _status, stored = await idempotent(principal, key, "POST", path, payload, handler)
    return stored


async def read_status(principal: Principal, listing_id: uuid.UUID) -> Any:
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        listing = await uow.listings.get(listing_id)
    if listing is None:
        raise NotFoundError("listing_id")
    return listing


def accepted_body(accepted: ItemAccepted) -> dict[str, Any]:
    """The 202 `JobAccepted` body."""
    from mkos_schemas.jobs import JobAccepted

    return JobAccepted(
        job_id=accepted.job_id,
        listing_id=accepted.listing_id,
        status=accepted.status,
        poll_url=f"/api/v1/jobs/{accepted.job_id}",
    ).model_dump(mode="json")
