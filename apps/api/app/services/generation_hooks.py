"""The `listing_generate` job hooks (S18 T17; D11; CP5; DATABASE.md:1384, :1790).

- `on_generate_terminal(session, job, outcome)`: in the terminal transaction (the wrapper's, the
  reaper's or the API cancel's), after the terminal write. It branches on the final `job.status`,
  never on `outcome` (None on every path but success): a job that ended `failed`, `dead_letter`
  or `cancelled` while its listing is still `generating` and owned by it (the newest
  `listing_generate` job of the listing) moves the listing to `failed/generation_failed`, with
  the job as actor. Anything else is a no-op. It invents no `agent_runs` and leaves the budget
  reservation as it is (a timeout or crash stays counted, DATABASE.md:998).
- `on_generate_requeue(session, old, new)`: refused (CP5). A dead-lettered generation's listing is
  already `failed/generation_failed`, and only a user `regenerate` (with its decision row) may
  send it back to `generating`; the staff CLI exits 4.
"""

from __future__ import annotations

from typing import Any, Final

from app.core.logging import get_logger
from app.db.models.ops import JobRun
from app.repositories.jobs import NotEligibleError
from app.repositories.listings import JobActor, ListingRepository
from app.workers.registry import Outcome

log = get_logger(__name__)
ENDED_WITHOUT_LISTING: Final = frozenset({"failed", "dead_letter", "cancelled"})


async def on_generate_terminal(session: Any, job: JobRun, outcome: Outcome | None) -> None:
    del outcome  # S13 `Hook`: branch on the final status only
    if job.status not in ENDED_WITHOUT_LISTING or job.listing_id is None:
        return
    listings = ListingRepository(session, job.tenant_id)
    listing = await listings.get_for_update(job.listing_id)
    if listing is None or listing.status != "generating":
        return
    if await listings.latest_generate_job_id(listing.id) != job.id:
        return  # a newer generation owns the listing
    await listings.move(
        listing,
        to="failed",
        event="generation_failed",
        reason="generation_failed",
        actor=JobActor(job.id),
    )
    log.info("generation.listing_failed", job_run_id=str(job.id), job_status=job.status)


async def on_generate_requeue(session: Any, old: JobRun, new: JobRun) -> None:
    del session, new
    log.info("generation.requeue_refused", job_run_id=str(old.id))
    raise NotEligibleError("regenerate through the API")
