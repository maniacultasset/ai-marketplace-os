"""The candidate job hooks (SI7 T11, design §2.3), registered on `image_compliance`.

- `on_image_terminal(session, job, outcome)`: in the terminal transaction. For a candidate job
  (`entity_type='image_candidate'`) that ended other than `succeeded` (a timeout, a dead letter,
  a cancel, a body exception, retry exhaustion), a candidate still `queued` under this job moves
  to `failed/model_failed`: retryable, outside the cache index, so the seller may ask again. The
  write is fenced on `job_id`, so an old job never touches a newer job's row. An upload job
  (`entity_type='image'`) is untouched.
- `on_image_requeue(session, old, new)`: a candidate job is never requeued by staff (the seller
  asks again through the API; a paid model call must not repeat silently). Upload jobs keep the
  staff requeue.
"""

from __future__ import annotations

from typing import Any, Final

from app.core.logging import get_logger
from app.db.models.ops import JobRun
from app.repositories.image_candidates import ImageCandidateRepository
from app.repositories.jobs import NotEligibleError
from app.workers.policy import CANDIDATE_ENTITY
from app.workers.registry import Outcome

log = get_logger(__name__)
CANDIDATE_FAILED: Final = "model_failed"


async def on_image_terminal(session: Any, job: JobRun, outcome: Outcome | None) -> None:
    del outcome  # branch on the final status only
    if job.entity_type != CANDIDATE_ENTITY or job.status == "succeeded":
        return
    moved = await ImageCandidateRepository(session, job.tenant_id).mark_failed(
        job.entity_id, job.id, CANDIDATE_FAILED
    )
    if moved:
        log.info(
            "image_candidate.failed",
            candidate_id=str(job.entity_id),
            job_run_id=str(job.id),
            reason=CANDIDATE_FAILED,
            job_status=job.status,
        )


async def on_image_requeue(session: Any, old: JobRun, new: JobRun) -> None:
    del session, new
    if old.entity_type != CANDIDATE_ENTITY:
        return
    log.info("image_candidate.requeue_refused", job_run_id=str(old.id))
    raise NotEligibleError("ask for the candidate again through the API")
