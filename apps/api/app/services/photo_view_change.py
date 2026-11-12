"""Move a saved photo to another view without uploading it again (client feedback 7, bullet 3).

`change_view(principal, image_id, slot, role)` re-labels one live photo: Front is `main`, Back and
Close-up a `secondary` with that role, a plain `secondary` an extra photo, and `lifestyle`,
`size_chart` and `packaging_label` their own slots (`photo_views.VIEW_SLOTS`). The rules:

- a role only with `slot: secondary` (422 at `role`); a size chart only on a style or a standalone
  (422 at `slot`); another tenant's, an unknown or a deleted photo is 404;
- Front, Back and Close-up hold one photo per product and colourway. When the target is taken,
  the two photos swap: the holder takes the moved photo's old view (a Front moved to Back puts
  the old Back in front). The other views hold many photos, so nothing swaps;
- the same view again changes nothing and writes nothing (200 with no job);
- a photo whose slot changed is checked again: its report is cleared, its status is `pending`
  and one `image_compliance` job is queued (the rules differ per slot, and the job then queues
  the photo read and the photo match as an upload does). A role-only move (Back to Close-up, an
  extra photo marked Back) keeps the checks and asks for the photo read again, which reads
  nothing when the photo set is unchanged;
- a photo moved where the photo match never looks (size chart, packaging label) drops its match;
  a photo that leaves Front retires its open image candidates (as a replace or a delete does);
- one `image.view_changed` audit row per photo moved: before and after `{slot}` (the image
  allow-list), the views in `reason` (`front->back`, not personal).

One transaction under the tenant image-job lock, then the product lock (the upload's order), so
an upload, a delete and a move of one product never interleave. The live-job cap counts the jobs
this call queues (429). The approval gate and the photo checklist read the live slot and role, so
both follow at once; a listing version keeps its photo ids, and the move takes effect in it with
no regenerate.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ImageSlot, JobKind

from app.core.errors import InputError, NotFoundError
from app.core.logging import get_logger
from app.core.rbac import Principal
from app.repositories.jobs import EnqueueSpec
from app.repositories.uow import TenantUnitOfWork, tenant_uow
from app.services.images import _check_product, delete_candidate_objects, enforce_live_image_cap
from app.services.photo_match import MATCH_SLOTS
from app.services.photo_read_upload import queue_photo_read
from app.services.photo_views import view_of

__all__ = ["MovedImage", "ViewChange", "change_view", "view_name"]

log = get_logger(__name__)
_EXTRA = "extra"  # a plain `secondary`: an extra photo with no view of its own


@dataclass(frozen=True, slots=True)
class MovedImage:
    image_id: uuid.UUID
    slot: ImageSlot
    role: str | None
    compliance_status: str
    # the re-check job, when the slot changed
    job_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class ViewChange:
    image: MovedImage
    # the photo that held the target view and took the moved photo's old one
    swapped: MovedImage | None


def view_name(slot: str, role: str | None) -> str:
    """`front`, `back`, `close_up`, `size_chart`, `lifestyle`, `packaging_label` or `extra`."""
    return view_of(slot, role) or _EXTRA


async def _queue_check(
    uow: TenantUnitOfWork, principal: Principal, image_id: uuid.UUID
) -> uuid.UUID:
    enqueued = await uow.job_runs.enqueue(
        EnqueueSpec(
            kind=JobKind.IMAGE_COMPLIANCE,
            entity_type="image",
            entity_id=image_id,
            created_by=principal.user_id,
            payload={"image_id": str(image_id)},
        )
    )
    return enqueued.job.id


@dataclass(frozen=True, slots=True)
class _Held:
    """A photo's view as read under the lock, before any write moves it."""

    id: uuid.UUID
    product_id: uuid.UUID
    colourway_id: uuid.UUID | None
    slot: ImageSlot
    role: str | None
    position: int
    compliance_status: str

    @classmethod
    def of(cls, row: Any) -> _Held:
        return cls(
            id=row.id,
            product_id=row.product_id,
            colourway_id=row.colourway_id,
            slot=ImageSlot(row.slot),
            role=row.role,
            position=row.position,
            compliance_status=row.compliance_status,
        )


async def _place(
    uow: TenantUnitOfWork,
    principal: Principal,
    held: _Held,
    slot: ImageSlot,
    role: str | None,
    candidate_keys: list[str],
) -> MovedImage:
    """One photo to (slot, role): the row, its re-check job, the candidates of a Front it leaves
    and the audit row."""
    recheck = held.slot is not slot
    position = (
        0
        if slot is ImageSlot.MAIN
        else await uow.images.next_position(held.product_id, held.colourway_id, slot)
    )
    if not await uow.images.set_view(
        held.id,
        slot=slot,
        role=role,
        position=position,
        recheck=recheck,
        drop_match=slot.value not in MATCH_SLOTS,
    ):
        raise NotFoundError("image_id")  # unreachable under the product lock
    if held.slot is ImageSlot.MAIN and slot is not ImageSlot.MAIN:
        candidate_keys.extend(await uow.image_candidates.fail_for_source(held.id))
    job_id = await _queue_check(uow, principal, held.id) if recheck else None
    await uow.audit.write(
        tenant_id=principal.tenant_id,
        actor_type="user",
        actor_user_id=principal.user_id,
        action="image.view_changed",
        entity_type="image",
        entity_id=held.id,
        before=json.dumps({"slot": held.slot.value}),
        after=json.dumps({"slot": slot.value}),
        reason=f"{view_name(held.slot.value, held.role)}->{view_name(slot.value, role)}",
    )
    return MovedImage(
        image_id=held.id,
        slot=slot,
        role=role,
        compliance_status="pending" if recheck else held.compliance_status,
        job_id=job_id,
    )


async def change_view(
    principal: Principal, image_id: uuid.UUID, slot: ImageSlot, role: str | None
) -> ViewChange:
    if role is not None and slot is not ImageSlot.SECONDARY:
        raise InputError(ErrorCode.VALIDATION_FAILED, "role")
    candidate_keys: list[str] = []
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        found = await uow.images.get_live(image_id)
        if found is None:
            raise NotFoundError("image_id")
        await uow.images.lock_tenant_image_jobs()
        await uow.images.lock_product_images(found.product_id)
        live = await uow.images.get_live(image_id)  # re-read under the lock
        if live is None:
            raise NotFoundError("image_id")
        row = _Held.of(live)
        if (row.slot, row.role) == (slot, role):
            unchanged = MovedImage(row.id, slot, role, row.compliance_status, None)
            return ViewChange(image=unchanged, swapped=None)
        await _check_product(uow.images, uow.products, row.product_id, slot)
        found_holder = await uow.images.view_holder(
            row.product_id, row.colourway_id, slot, role, other_than=row.id
        )
        holder = None if found_holder is None else _Held.of(found_holder)
        jobs = int(row.slot is not slot) + int(holder is not None and holder.slot is not row.slot)
        if jobs:
            await enforce_live_image_cap(uow.images, adding=jobs)
        if holder is not None:
            # A plain secondary holds no unique view, so the holder parks there while the moved
            # photo takes the target (`images_one_main`, `images_one_role` hold per statement).
            await uow.images.set_view(
                holder.id, slot=ImageSlot.SECONDARY, role=None, position=holder.position
            )
        moved = await _place(uow, principal, row, slot, role, candidate_keys)
        swapped = None
        if holder is not None:
            swapped = await _place(uow, principal, holder, row.slot, row.role, candidate_keys)
    await delete_candidate_objects(candidate_keys)
    if moved.job_id is None:
        await _ask_photo_read(principal, image_id)
    log.info(
        "image.view_changed",
        tenant_id=str(principal.tenant_id),
        image_id=str(image_id),
        swapped=None if swapped is None else str(swapped.image_id),
    )
    return ViewChange(image=moved, swapped=swapped)


async def _ask_photo_read(principal: Principal, image_id: uuid.UUID) -> None:
    """A role-only move: the photo read reads the photos in view order, so it is asked again
    (nothing is queued for a photo set already read). A failed queue never fails the move."""
    try:
        async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
            await queue_photo_read(uow, image_id)
    except Exception as error:
        log.warning("photo_read.queue_failed", image_id=str(image_id), error=type(error).__name__)
