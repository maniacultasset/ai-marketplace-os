"""Photos from spreadsheet links (G2): plan, store, retry and show the links of a bulk import.

While `SHEET_PHOTOS_ENABLED` is on, a bulk import plans each row's `main_image` and
`additional_images` links (`plan_row`, in the row's savepoint, never a network call) and queues one
background download per target product (`enqueue_fetch`, an `image_compliance` job about a
`photo_links` entity). `app.workers.photo_links` downloads them and stores each one here through
the upload path (`save_fetched`). Decisions (PR G2):

- D2 targets: a standalone or style row's links go on that product; a variant row's on its style
  and colourway when the family is split by colour, else on the style. Dedupe is per (tenant,
  target, colourway, sha256 of the direct link), so S/M/L rows of one colour that share a link
  fetch it once, and the first row of the file that named it owns it.
- D3 views, chosen when the photo is saved: the first `main_image` link becomes the Front only
  while that product/colourway has no live Front; further links fill the free Back, Close-up and
  Lifestyle views the category marks required or recommended (`photo_views.merged`, while
  `PHOTO_VIEWS_ENABLED` is on), in that order; the rest are extra photos the seller can move
  (`PATCH /images/{image_id}`). A seller's photo is never replaced.
- D4 one transaction in the upload's lock order (the tenant image-job lock, the product lock),
  the live-job cap, the 50-photo cap, then `insert_image_and_job` (origin `seller`), so the
  white-background, text, QA, photo read and photo match checks follow exactly as for an upload.
  The audit actor is the member who imported the file (or pressed Retry).
- D6 a re-import requeues failed and capped links, keeps saved ones, and never downloads again a
  photo the seller saved and then deleted (`skipped/removed_by_seller`).
- Links past `SHEET_PHOTOS_MAX_PER_IMPORT` in one run are `skipped/import_cap` (retryable).

The seller sees each link's result in the import report row (`rows_photos`) and on the product
(`product_photo_links`); `message` is the plain sentence, without the "Photo N:" prefix the
dashboard adds. The link itself is never returned, logged or put in an error.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ImageSlot, JobKind
from mkos_schemas.imports import ImportPhotoTotals, PhotoLinkResult

from app.core.config import get_settings
from app.core.errors import ConflictError, DomainError, NotFoundError
from app.core.logging import get_logger
from app.core.rbac import Principal
from app.db.models.catalog_assets import PhotoLink
from app.repositories.images import NewImage
from app.repositories.jobs import EnqueueSpec
from app.repositories.photo_links import RETRYABLE_SKIPS, PlannedLink
from app.repositories.transactions import RowDbError, row_savepoint
from app.repositories.uow import TenantUnitOfWork, repositories_on, tenant_uow
from app.services import photo_views
from app.services.idempotency import idempotent
from app.services.images import (
    LIVE_IMAGE_JOB_CAP,
    MAX_LIVE_IMAGES_PER_PRODUCT,
    ImageActor,
    insert_image_and_job,
)
from app.services.share_links import direct_url, is_drive

log = get_logger(__name__)
API_V1: Final = "/api/v1"
ENTITY: Final = "photo_links"
LIVE: Final = ("queued", "running")
# D3: the views a sheet link may fill after the Front, in this order (never the size chart or
# the packaging label)
AUTO_VIEWS: Final[tuple[photo_views.PhotoView, ...]] = ("back", "close_up", "lifestyle")
WANTED: Final = frozenset({"required", "recommended"})
WIRE_STATUS: Final = {
    "queued": "queued",
    "fetching": "downloading",
    "saved": "saved",
    "failed": "failed",
    "skipped": "skipped",
}
FILE_TYPES: Final = {"webp": "WebP", "gif": "GIF", "tiff": "TIFF", "heic": "HEIC", "bmp": "BMP"}
MESSAGES: Final[Mapping[str, str]] = {
    "not_found": "The link returned 404. Check the link is right and public.",
    "not_public": "The link is not public. Share it so anyone with the link can view, then retry.",
    "not_image": (
        "The link opens a web page, not a photo. Use a link that opens the JPG or PNG itself."
    ),
    "too_large": "The photo is over 10 MB. Use a smaller file.",
    "too_many_pixels": "The photo is too big to read. Use one under 40 megapixels.",
    "corrupt": "The file could not be read as a photo.",
    "timeout": "The site took too long to answer. Retry later.",
    "unreachable": "The site could not be reached. Check the link.",
    "blocked_address": "This link points to a private network, so it can't be downloaded.",
    "too_many_redirects": "The link redirects too many times. Use the photo's direct link.",
    "photo_limit": f"This product already has {MAX_LIVE_IMAGES_PER_PRODUCT} photos.",
    "import_cap": (
        "This file has more than {cap} photo links, so this one was not downloaded. "
        "Import the file again to get the rest."
    ),
    "removed_by_seller": "You removed this photo earlier, so it was not downloaded again.",
    "product_gone": "The product was deleted, so this photo was not downloaded.",
}
DRIVE_NOT_PUBLIC: Final = (
    "Google Drive did not hand over the photo. Share it as 'Anyone with the link', then retry."
)
SAVED_THEN_REMOVED: Final = "Saved, then removed from the product."
# the fetch leaves the seller's own uploads this much room under LIVE_IMAGE_JOB_CAP
UPLOAD_HEADROOM: Final = 5


class SheetPhotosOff(DomainError):  # noqa: N818 - a refusal, the route answers 501
    """`SHEET_PHOTOS_ENABLED` is off."""

    MESSAGE: Final = "Photos from spreadsheet links are not switched on."


def enabled() -> bool:
    return get_settings().sheet_photos_enabled


def link_hash(url: str) -> str:
    """sha256 hex of the direct-download form of `url` (the dedupe key)."""
    return hashlib.sha256(direct_url(url).encode("utf-8")).hexdigest()


# ---- the import's plan (D2, D6) ----


@dataclass
class ImportPlan:
    """One import run's links: its cap and the products that need a download job."""

    import_id: uuid.UUID
    requested_by: uuid.UUID | None
    cap: int
    planned: int = 0
    targets: set[uuid.UUID] = field(default_factory=set)


def new_plan(import_id: uuid.UUID, requested_by: uuid.UUID | None) -> ImportPlan | None:
    """The run's plan while the switch is on, else None (nothing is written, as before G2)."""
    settings = get_settings()
    if not settings.sheet_photos_enabled:
        return None
    return ImportPlan(import_id, requested_by, settings.sheet_photos_max_per_import)


def _requeueable(row: PhotoLink) -> bool:
    if row.status in ("failed", "dismissed"):
        return True
    return row.status == "skipped" and row.problem in (*RETRYABLE_SKIPS, "product_gone")


async def _plan_one(
    uow: TenantUnitOfWork, plan: ImportPlan, planned: PlannedLink
) -> uuid.UUID | None:
    """One link of a row; the target product when it needs a download job."""
    links = uow.photo_links
    existing = await links.find(planned.product_id, planned.colourway_id, planned.url_hash)
    if existing is not None and existing.import_id == plan.import_id:
        return None  # an earlier row of this file named it: that row owns it
    over_cap = plan.planned >= plan.cap
    if existing is None:
        status, problem = ("skipped", "import_cap") if over_cap else ("queued", None)
        if await links.insert(planned, status=status, problem=problem) is None:
            return None  # a concurrent writer holds it
    elif _requeueable(existing):
        status, problem = ("skipped", "import_cap") if over_cap else ("queued", None)
        await links.replan(existing.id, planned, status=status, problem=problem)
    elif existing.status == "saved":
        live = existing.image_id is not None and await uow.images.get_live(existing.image_id)
        if live:
            await links.replan(existing.id, planned, status=None)
        else:
            await links.replan(existing.id, planned, status="skipped", problem="removed_by_seller")
        return None
    elif existing.status in ("queued", "fetching"):
        await links.replan(existing.id, planned, status=None)
        return planned.product_id  # make sure a download job is on its way
    else:  # skipped as removed by the seller: never downloaded again
        await links.replan(existing.id, planned, status=None)
        return None
    if over_cap:
        return None
    plan.planned += 1
    return planned.product_id


async def plan_row(
    session: Any,
    tenant_id: uuid.UUID,
    plan: ImportPlan,
    *,
    product_id: uuid.UUID,
    colourway_id: uuid.UUID | None,
    image_urls: Sequence[str],
    main_image_count: int,
    row_number: int,
) -> set[uuid.UUID]:
    """The row's links as `photo_links` rows (no network); the products that need a download.
    In its own savepoint inside the row's: a refused link write is logged and dropped, and the
    row still imports (a link never fails a row)."""
    targets: set[uuid.UUID] = set()
    if not image_urls:
        return targets
    uow = repositories_on(session, tenant_id)
    before = plan.planned
    try:
        async with row_savepoint(session):
            for position, url in enumerate(image_urls[:9], start=1):
                planned = PlannedLink(
                    product_id=product_id,
                    colourway_id=colourway_id,
                    url=url,
                    url_hash=link_hash(url),
                    source_column=(
                        "main_image" if position <= main_image_count else "additional_images"
                    ),
                    position=position,
                    import_id=plan.import_id,
                    row_number=row_number,
                    requested_by=plan.requested_by,
                )
                target = await _plan_one(uow, plan, planned)
                if target is not None:
                    targets.add(target)
    except RowDbError as exc:
        plan.planned = before
        log.warning(
            "photo_links.plan_refused",
            import_id=str(plan.import_id),
            row_number=row_number,
            sqlstate=exc.sqlstate,
            constraint=exc.constraint,
        )
        return set()
    return targets


async def enqueue_fetch(
    uow: TenantUnitOfWork,
    product_id: uuid.UUID,
    *,
    created_by: uuid.UUID | None,
    import_id: uuid.UUID | None = None,
) -> Any:
    """The product's download job: a queued one already waiting is reused; a running one gets a
    successor (it claimed its links before these), which waits under the tenant's lock."""
    latest = await uow.job_runs.latest_for_entity(ENTITY, product_id)
    if latest is not None and latest.status == "queued":
        return latest
    payload: dict[str, Any] = {"photo_links": str(product_id)}
    if import_id is not None:
        payload["import_id"] = str(import_id)
    enqueued = await uow.job_runs.enqueue(
        EnqueueSpec(
            kind=JobKind.IMAGE_COMPLIANCE,
            entity_type=ENTITY,
            entity_id=product_id,
            created_by=created_by,
            payload=payload,
        )
    )
    return enqueued.job


# ---- the views a fetched photo fills (D3), pure ----


ViewChoice = Literal["front", "back", "close_up", "lifestyle"] | None


def taken_views(live: Iterable[tuple[str, str | None]]) -> set[str]:
    """The one-of views already held among live (slot, role) pairs, plus `lifestyle` when any
    lifestyle photo is live (a sheet link never piles onto a filled view)."""
    taken: set[str] = set()
    for slot, role in live:
        view = photo_views.view_of(slot, role)
        if view is not None:
            taken.add(view)
    return taken


def wanted_views(category: str | None, *, views_enabled: bool) -> tuple[str, ...]:
    """The views after the Front a link may fill: required or recommended for the category."""
    if not views_enabled:
        return ()
    marks = {v.view: v.requirement for v in photo_views.merged(category)}
    return tuple(v for v in AUTO_VIEWS if marks.get(v) in WANTED)


def choose_view(*, is_main: bool, taken: set[str], wanted: Sequence[str]) -> ViewChoice:
    """D3 for one link: the Front for the first `main_image` link while it is free (else an
    extra photo); any other link the first wanted view still free; else None (an extra)."""
    if is_main:
        return "front" if "front" not in taken else None
    for view in wanted:
        if view not in taken:
            return view  # type: ignore[return-value]  # AUTO_VIEWS members
    return None


def plan_views(links: Sequence[bool], taken: set[str], wanted: Sequence[str]) -> list[ViewChoice]:
    """`choose_view` over a product's links in order (`True` = the first main link), each
    choice taking its view for the links after it."""
    held = set(taken)
    out: list[ViewChoice] = []
    for is_main in links:
        view = choose_view(is_main=is_main, taken=held, wanted=wanted)
        if view is not None:
            held.add(view)
        out.append(view)
    return out


# ---- storing one fetched photo (D4) ----


SaveOutcome = Literal["saved", "photo_limit", "no_room", "gone"]


@dataclass(frozen=True, slots=True)
class LinkActor:
    """The member a sheet photo is stored for (the importer, or who pressed Retry)."""

    tenant_id: uuid.UUID
    user_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class Saved:
    outcome: SaveOutcome
    view: str | None = None
    image_job_id: uuid.UUID | None = None


async def save_fetched(
    uow: TenantUnitOfWork,
    actor: ImageActor,
    link: PhotoLink,
    *,
    fetch_job_id: uuid.UUID,
    image_id: uuid.UUID,
    object_key: str,
    size: int,
) -> Saved:
    """In the caller's transaction, after the object is stored: the upload's locks and caps,
    the view (D3), the image row with its check job and audit, and the link `saved`."""
    await uow.images.lock_tenant_image_jobs()
    await uow.images.lock_product_images(link.product_id)
    current = await uow.photo_links.get_link(link.id)
    if current is None or current.status != "fetching" or current.job_id != fetch_job_id:
        return Saved("gone")
    product = await uow.products.get_by_id(link.product_id)
    if product is None:
        return Saved("gone")
    if (
        link.colourway_id is not None
        and await uow.images.colourway_style(link.colourway_id) != link.product_id
    ):
        return Saved("gone")
    if await uow.images.count_live_image_jobs() + 1 > LIVE_IMAGE_JOB_CAP:
        return Saved("no_room")
    if await uow.images.count_live(link.product_id) >= MAX_LIVE_IMAGES_PER_PRODUCT:
        return Saved("photo_limit")
    taken = taken_views(await uow.images.live_views(link.product_id, link.colourway_id))
    wanted = wanted_views(product.category, views_enabled=photo_views.enabled())
    is_main = link.source_column == "main_image" and link.position == 1
    view = choose_view(is_main=is_main, taken=taken, wanted=wanted)
    slot, role = photo_views.VIEW_SLOTS[view] if view is not None else (ImageSlot.SECONDARY, None)
    position = (
        0
        if slot is ImageSlot.MAIN
        else await uow.images.next_position(link.product_id, link.colourway_id, slot)
    )
    image_job = await insert_image_and_job(
        uow,
        actor,
        NewImage(
            id=image_id,
            product_id=link.product_id,
            colourway_id=link.colourway_id,
            storage_key=object_key,
            slot=slot,
            bytes=size,
            position=position,
            role=role,
        ),
        field="photo_links",
    )
    if not await uow.photo_links.mark_saved(link.id, fetch_job_id, image_id):
        raise ConflictError(ErrorCode.INVALID_STATUS_TRANSITION, "photo_links")
    return Saved("saved", view or "extra", image_job)


async def room_for_photos(uow: TenantUnitOfWork) -> bool:
    """The tenant's live image jobs leave the seller's uploads `UPLOAD_HEADROOM` free slots."""
    return await uow.images.count_live_image_jobs() < LIVE_IMAGE_JOB_CAP - UPLOAD_HEADROOM


# ---- what the seller sees ----


def _view_name(slot: str, role: str | None) -> str:
    return photo_views.view_of(slot, role) or "extra"


def message_for(row: PhotoLink, *, cap: int | None = None) -> str | None:
    """The plain seller sentence of a failed or skipped link (no "Photo N:" prefix)."""
    problem = row.problem
    if problem is None:
        return None
    if problem == "unsupported_type":
        label = FILE_TYPES.get(row.file_type or "")
        if label is None:
            return "This file type can't be used as a photo. Use a JPG or PNG."
        return f"This photo is a {label} file. Use a JPG or PNG."
    if problem == "server_error":
        if row.http_status is not None:
            return f"The site had a problem (error {row.http_status}). Retry later."
        return "The site had a problem. Retry later."
    if problem == "not_public" and is_drive(row.url):
        return DRIVE_NOT_PUBLIC
    if problem == "import_cap":
        limit = cap if cap is not None else get_settings().sheet_photos_max_per_import
        return MESSAGES["import_cap"].format(cap=f"{limit:,}")
    return MESSAGES.get(problem)


def result_view(
    row: PhotoLink, slots: Mapping[uuid.UUID, tuple[str, str | None]]
) -> PhotoLinkResult:
    """One link as the wire `PhotoLinkResult`; `slots` holds the live saved images' views."""
    status = WIRE_STATUS.get(row.status, "skipped")
    view: str | None = None
    message: str | None = None
    if row.status == "saved":
        held = slots.get(row.image_id) if row.image_id is not None else None
        if held is not None:
            view = _view_name(*held)
        else:
            message = SAVED_THEN_REMOVED
    elif row.status in ("failed", "skipped"):
        message = message_for(row)
    retryable = row.status == "failed" or (
        row.status == "skipped" and row.problem in RETRYABLE_SKIPS
    )
    return PhotoLinkResult.model_validate(
        {
            "link_id": row.id,
            "position": row.position,
            "column": row.source_column,
            "status": status,
            "view": view,
            "image_id": row.image_id if row.status == "saved" else None,
            "colourway_id": row.colourway_id,
            "problem": row.problem,
            "http_status": row.http_status,
            "message": message,
            "retryable": retryable,
        }
    )


async def _results(uow: TenantUnitOfWork, rows: Sequence[PhotoLink]) -> list[PhotoLinkResult]:
    saved = [r.image_id for r in rows if r.status == "saved" and r.image_id is not None]
    slots = await uow.images.live_slots(saved)
    return [result_view(r, slots) for r in rows]


@dataclass(frozen=True, slots=True)
class ProductLinks:
    links: list[PhotoLinkResult]
    pending: bool


async def product_photo_links(uow: TenantUnitOfWork, product_id: uuid.UUID) -> ProductLinks:
    """A product's links for its detail read (a style's colourway links included)."""
    rows = await uow.photo_links.for_product(product_id)
    pending = any(r.status in ("queued", "fetching") for r in rows)
    return ProductLinks(await _results(uow, rows), pending)


async def rows_photos(
    uow: TenantUnitOfWork, import_id: uuid.UUID, row_numbers: Sequence[int]
) -> dict[int, list[PhotoLinkResult]]:
    """Each import row's links, in one SELECT for the page (and one for the saved views)."""
    by_row = await uow.photo_links.for_import_rows(import_id, row_numbers)
    flat = [r for rows in by_row.values() for r in rows]
    saved = [r.image_id for r in flat if r.status == "saved" and r.image_id is not None]
    slots = await uow.images.live_slots(saved)
    return {n: [result_view(r, slots) for r in rows] for n, rows in by_row.items()}


async def import_totals(uow: TenantUnitOfWork, import_id: uuid.UUID) -> ImportPhotoTotals | None:
    totals = await uow.photo_links.totals_for_import(import_id)
    if totals is None:
        return None
    return ImportPhotoTotals(
        total=totals.total, waiting=totals.waiting, saved=totals.saved, failed=totals.failed
    )


# ---- the seller's retry and dismiss ----


def _nothing_to_retry() -> ConflictError:
    return ConflictError(ErrorCode.INVALID_STATUS_TRANSITION, "photo_links")


async def retry_product(
    principal: Principal, product_id: uuid.UUID, key: str, path: str
) -> dict[str, Any]:
    """`POST /products/{product_id}/photo-links/retry`: the product's failed and capped links back
    in the queue and its download job (202 `JobAccepted`); 409 when none can be retried."""
    if not enabled():
        raise SheetPhotosOff(SheetPhotosOff.MESSAGE)
    tenant = principal.tenant_id

    async def handler(session: Any) -> tuple[int, dict[str, Any]]:
        uow = repositories_on(session, tenant)
        if await uow.products.get_by_id(product_id) is None:
            raise NotFoundError("product_id")
        requeued = await uow.photo_links.requeue_failed(
            product_id=product_id, requested_by=principal.user_id
        )
        if not requeued:
            raise _nothing_to_retry()
        job = await enqueue_fetch(uow, product_id, created_by=principal.user_id)
        log.info("photo_links.retry", product_id=str(product_id), links=len(requeued))
        return 202, {
            "job_id": str(job.id),
            "listing_id": None,
            "status": job.status,
            "estimated_duration_seconds": 30,
            "poll_url": f"{API_V1}/jobs/{job.id}",
        }

    _status, body = await idempotent(principal, key, "POST", path, b"", handler)
    return body


async def retry_import(
    principal: Principal, import_id: uuid.UUID, key: str, path: str
) -> dict[str, Any]:
    """`POST /imports/{import_id}/photo-links/retry`: every failed and capped link of the import
    back in the queue, one download job per product (202 `{queued, products}`)."""
    if not enabled():
        raise SheetPhotosOff(SheetPhotosOff.MESSAGE)
    tenant = principal.tenant_id

    async def handler(session: Any) -> tuple[int, dict[str, Any]]:
        from app.repositories.imports import ImportRepository

        uow = repositories_on(session, tenant)
        if await ImportRepository(session, tenant).get(import_id) is None:
            raise NotFoundError("import_id")
        requeued = await uow.photo_links.requeue_failed(
            import_id=import_id, requested_by=principal.user_id
        )
        if not requeued:
            raise _nothing_to_retry()
        products = sorted({p for _link, p in requeued}, key=str)
        for product in products:
            if await uow.products.get_by_id(product) is None:
                await uow.photo_links.cancel_for_products([product])
                continue
            await enqueue_fetch(uow, product, created_by=principal.user_id, import_id=import_id)
        log.info("photo_links.retry_import", import_id=str(import_id), links=len(requeued))
        return 202, {"queued": len(requeued), "products": len(products)}

    _status, body = await idempotent(principal, key, "POST", path, b"", handler)
    return body


async def dismiss(principal: Principal, link_id: uuid.UUID) -> None:
    """`DELETE /photo-links/{link_id}`: hide a failed or skipped link (409 for any other)."""
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        row = await uow.photo_links.get_link(link_id)
        if row is None:
            raise NotFoundError("link_id")
        if not await uow.photo_links.dismiss(link_id):
            raise _nothing_to_retry()
    log.info("photo_links.dismissed", link_id=str(link_id))
