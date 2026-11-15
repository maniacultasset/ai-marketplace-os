"""The sheet photo download job body (G2): `run_photo_links(ctx)`.

An `image_compliance` job (`entity_type='photo_links'`, entity = the target product) whose payload
names `photo_links` (`app.workers.image_jobs` dispatches). It runs under `photo_link:<tenant>`, so
one brand downloads one product's links at a time and a waiting job holds no worker slot.

1. `SHEET_PHOTOS_ENABLED` off: `succeeded {skipped: off}`; the links stay `queued`.
2. The product gone: its waiting links are `skipped/product_gone`.
3. Claim the links (`queued`, or this job's own `fetching` ones after a crash), one commit.
4. Per link, while `DEADLINE_S` (240 s of the 300 s policy timeout) leaves room for one more:
   a. wait (2 s polls) until the tenant's live image jobs leave the seller's uploads
      `UPLOAD_HEADROOM` slots, so an import never makes an upload answer 429;
   b. download it (`app.adapters.photo_fetch`, the only code that opens a link; never in a
      transaction);
   c. a photo: the object is stored first (as an upload's), then `save_fetched` in one
      transaction stores the image through the upload path and marks the link `saved`; over the
      50-photo cap the object is deleted and the link `failed/photo_limit`;
   d. a failure: the link `failed` with its problem.
   Out of time or of upload room, the rest go back to `queued` for the sweeper
   (`schedule_photo_links`, every 2 minutes).
5. `succeeded`, or `partial` with `{saved, failed, deferred}`.

A body exception or a timeout ends the job through the policy; the terminal hook
(`app.services.image_candidate_hooks`) then releases this job's `fetching` links. Logs carry the
link id, the problem, the bytes and the duration only, never the link.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from typing import Any, Final

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import JobKind

from app.adapters.photo_fetch import FetchFailure, FetchOk, PhotoFetcher
from app.adapters.storage import StorageUnavailable, get_storage, images_bucket
from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.models.catalog_assets import PhotoLink
from app.repositories.uow import tenant_uow
from app.services import photo_links as svc
from app.services.image_keys import image_object_key
from app.services.images import MULTIPART_CONTENT_TYPE
from app.services.share_links import direct_url
from app.workers.errors import JobFailed, system_report
from app.workers.policy import POLICIES
from app.workers.registry import JobContext, Outcome

log = get_logger(__name__)
DEADLINE_S: Final = 240.0
ROOM_POLL_S: Final = 2.0
# a link is started only while this much of the deadline is left (its 30 s cap plus the store)
LINK_BUDGET_S: Final = 40.0

# test seams: the fetcher and the clock
FetcherFactory = Callable[[], Any]


def default_fetcher() -> PhotoFetcher:
    raw = get_settings().sheet_photos_local_hosts or ""
    return PhotoFetcher(local_hosts=[h for h in raw.split(",") if h.strip()])


_fetcher_factory: FetcherFactory = default_fetcher


def set_fetcher_factory(factory: FetcherFactory | None) -> None:
    """Tests replace the fetcher (None restores the real one)."""
    global _fetcher_factory
    _fetcher_factory = factory or default_fetcher


def _failed(message: str, ctx: JobContext) -> JobFailed:
    return JobFailed(
        system_report(
            ErrorCode.INTERNAL_ERROR,
            message,
            attempt=ctx.attempt,
            max_attempts=POLICIES[JobKind.IMAGE_COMPLIANCE].max_attempts,
            request_id=ctx.request_id,
        )
    )


def _product(ctx: JobContext) -> uuid.UUID:
    try:
        return uuid.UUID(str(ctx.payload["photo_links"]))
    except (KeyError, ValueError):
        raise _failed("the photo link job names no product", ctx) from None


async def _release(ctx: JobContext, links: list[PhotoLink]) -> None:
    if not links:
        return
    async with tenant_uow(ctx.tenant_id) as uow:
        await uow.photo_links.release(ctx.job_run_id, [link.id for link in links])


async def _wait_for_room(ctx: JobContext, deadline: float) -> bool:
    loop = asyncio.get_running_loop()
    while True:
        async with tenant_uow(ctx.tenant_id) as uow:
            if await svc.room_for_photos(uow):
                return True
        if loop.time() + ROOM_POLL_S + LINK_BUDGET_S > deadline:
            return False
        await asyncio.sleep(ROOM_POLL_S)


async def _fail(ctx: JobContext, link: PhotoLink, failure: FetchFailure) -> None:
    async with tenant_uow(ctx.tenant_id) as uow:
        await uow.photo_links.mark_failed(
            link.id, ctx.job_run_id, failure.problem, failure.http_status, failure.kind
        )


async def _delete_object(key: str) -> None:
    try:
        await get_storage().delete(images_bucket(), key)
    except StorageUnavailable:
        log.info("photo_link.object_kept", reason="store_down")  # the orphan sweep removes it


async def _store(
    ctx: JobContext, link: PhotoLink, photo: FetchOk, actor: svc.LinkActor | None
) -> str:
    """The object, then the image through the upload path; the outcome."""
    image_id = uuid.uuid4()
    key = image_object_key(ctx.tenant_id, link.product_id, image_id)
    await get_storage().put(images_bucket(), key, photo.data, MULTIPART_CONTENT_TYPE)
    if actor is None:  # no member to store it for: never stored as nobody
        await _delete_object(key)
        await _fail(ctx, link, FetchFailure("unreachable"))
        return "failed"
    async with tenant_uow(ctx.tenant_id) as uow:
        saved = await svc.save_fetched(
            uow,
            actor,
            link,
            fetch_job_id=ctx.job_run_id,
            image_id=image_id,
            object_key=key,
            size=len(photo.data),
        )
    if saved.outcome == "saved":
        return "saved"
    await _delete_object(key)
    if saved.outcome == "photo_limit":
        async with tenant_uow(ctx.tenant_id) as uow:
            await uow.photo_links.mark_failed(link.id, ctx.job_run_id, "photo_limit")
        return "failed"
    return saved.outcome  # `no_room` or `gone`


async def run_photo_links(ctx: JobContext) -> Outcome:
    product_id = _product(ctx)
    if not svc.enabled():
        return Outcome("succeeded", {"skipped": "off"}, zero_reservation=True)
    async with tenant_uow(ctx.tenant_id) as uow:
        product = await uow.products.get_by_id(product_id)
        if product is None:
            gone = await uow.photo_links.cancel_for_products([product_id])
            return Outcome(
                "succeeded", {"skipped": "product_gone", "links": len(gone)}, zero_reservation=True
            )
        links = await uow.photo_links.claim(product_id, ctx.job_run_id)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + DEADLINE_S
    fetcher = _fetcher_factory()
    counts = {"saved": 0, "failed": 0, "deferred": 0}
    for index, link in enumerate(links):
        rest = links[index:]
        if loop.time() + LINK_BUDGET_S > deadline or not await _wait_for_room(ctx, deadline):
            await _release(ctx, rest)
            counts["deferred"] += len(rest)
            break
        started = loop.time()
        result = await fetcher.fetch(direct_url(link.url))
        actor = (
            svc.LinkActor(ctx.tenant_id, link.requested_by)
            if link.requested_by is not None
            else None
        )
        if isinstance(result, FetchFailure):
            await _fail(ctx, link, result)
            outcome = "failed"
        else:
            try:
                outcome = await _store(ctx, link, result, actor)
            except StorageUnavailable:
                outcome = "no_room"
        log.info(
            "photo_link.done",
            link_id=str(link.id),
            outcome=outcome,
            problem=result.problem if isinstance(result, FetchFailure) else None,
            bytes=len(result.data) if isinstance(result, FetchOk) else 0,
            ms=int((loop.time() - started) * 1000),
        )
        if outcome in ("saved", "failed"):
            counts[outcome] += 1
            continue
        if outcome == "gone":
            async with tenant_uow(ctx.tenant_id) as uow:
                if await uow.products.get_by_id(product_id) is None:
                    await uow.photo_links.cancel_for_products([product_id])
                    break
            continue
        # no upload room (or the store is down): this link and the rest wait for the sweeper
        await _release(ctx, rest)
        counts["deferred"] += len(rest)
        break
    result_json = {"product_id": str(product_id), **counts}
    if counts["failed"] or counts["deferred"]:
        return Outcome("partial", result_json, zero_reservation=True)
    return Outcome("succeeded", result_json, zero_reservation=True)
