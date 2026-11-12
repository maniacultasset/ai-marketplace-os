"""The seller applies Kuinn's suggested fixes (client item 20): `POST /listings/{id}/fixes/apply`.

With the brand's `auto_fix_enabled` off, the resolution executor keeps every fix as a suggestion
(a held repair, or the content fix or AI rewrite it would run) and the issue waits in "Needs you"
(`resolution.run`, step 8). This is the seller's go for one listing: under the listing lock it
runs the executor once with `approved_by` set, so the held repairs go live (audited
`listing_repair.approved`, the user as actor) and the re-check, resend or fix job follows exactly
as it would have with the setting on (the invariant 9 cap still counts it).

- the listing must exist in the caller's tenant (404), not be paused or archived and not be busy
  generating (409 `INVALID_STATUS_TRANSITION` / `LISTING_BUSY`);
- with no open marketplace ERROR for its adapter there is nothing to apply: 409
  `INVALID_STATUS_TRANSITION`;
- the run's entry follows the listing: `failed` -> `submission` (the resend), `published` ->
  `read`, anything else -> `preview` (the re-check of the current version). The job the newest
  open issue came from is the run's job (its id goes on the repairs and audit rows).

A second call releases nothing twice (`RepairRepository.release` only flips held rows) and the
executor's own one-try indexes keep it replay-safe.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

from mkos_schemas.codes import ErrorCode

from app.core.errors import ConflictError, NotFoundError
from app.core.rbac import Principal
from app.repositories.uow import tenant_uow
from app.services.issues import adapter_source
from app.services.resolution.run import Entry, resolve

__all__ = ["FixesApplied", "apply_fixes", "entry_for"]

BUSY: Final = frozenset({"generating", "publishing"})


@dataclass(frozen=True, slots=True)
class FixesApplied:
    released: int
    """Held repairs made live by this call."""
    job_id: uuid.UUID | None
    """The re-check, resend or fix job the executor enqueued, if any."""


def entry_for(status: str) -> Entry:
    """The executor entry a seller's apply runs under, from the listing's status."""
    if status == "failed":
        return "submission"
    if status == "published":
        return "read"
    return "preview"


async def apply_fixes(principal: Principal, listing_id: uuid.UUID) -> FixesApplied:
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        listing = await uow.listings.get_for_update(listing_id)
        if listing is None:
            raise NotFoundError("listing_id")
        if listing.paused_at is not None or listing.archived_at is not None:
            raise ConflictError(ErrorCode.INVALID_STATUS_TRANSITION, "listings")
        if listing.status in BUSY:
            raise ConflictError(ErrorCode.LISTING_BUSY, "listings")
        entry = entry_for(listing.status)
        rows = await uow.listing_issues.open_errors(
            listing.id,
            [adapter_source(listing.marketplace)],
            channel="preview" if entry == "preview" else "live",
        )
        with_job = [(r.last_seen_at, str(r.id), r.job_id) for r in rows if r.job_id is not None]
        if not with_job:
            raise ConflictError(ErrorCode.INVALID_STATUS_TRANSITION, "listing_issues")
        job_id = max(with_job, key=lambda r: (r[0], r[1]))[2]
        job = None if job_id is None else await uow.job_runs.get_by_id(job_id)
        if job is None:
            raise ConflictError(ErrorCode.INVALID_STATUS_TRANSITION, "job_runs")
        held_before = {
            r.id
            for r in await uow.listing_repairs.live_for_listing(listing.id, include_held=True)
            if r.held
        }
        result = await resolve(uow, listing, entry=entry, job=job, approved_by=principal.user_id)
        held_after = {
            r.id
            for r in await uow.listing_repairs.live_for_listing(listing.id, include_held=True)
            if r.held
        }
        return FixesApplied(
            released=len(held_before - held_after),
            job_id=None if result is None else result.job_id,
        )
