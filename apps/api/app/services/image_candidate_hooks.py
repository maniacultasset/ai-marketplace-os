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
- SI12: a size-chart read job (`entity_type='size_chart_read'`, entity = the image) that ended
  other than `succeeded` stores `metadata.size_chart_read = {status: failed}` on the image (CP-D4),
  fenced on the image's clean hash and never over another job's settled result, so the seller can
  ask again; its staff requeue is refused alike.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

from app.core.logging import get_logger
from app.db.models.ops import JobRun
from app.repositories.image_candidates import ImageCandidateRepository
from app.repositories.images import ImageRepository
from app.repositories.jobs import NotEligibleError
from app.workers.policy import CANDIDATE_ENTITY, SIZE_CHART_READ_ENTITY
from app.workers.registry import Outcome

log = get_logger(__name__)
CANDIDATE_FAILED: Final = "model_failed"


async def _size_chart_read_failed(session: Any, job: JobRun) -> None:
    images = ImageRepository(session, job.tenant_id)
    image = await images.get_live(job.entity_id)
    if image is None or image.clean_content_hash is None:
        return  # replaced or deleted: the suggestion follows the new photo
    written = await images.write_size_chart_read(
        image.id,
        {"status": "failed", "read_at": datetime.now(UTC).isoformat()},
        source_hash=image.clean_content_hash,
        job_id=job.id,
    )
    if written:
        log.info(
            "size_chart_read.failed",
            image_id=str(image.id),
            job_run_id=str(job.id),
            job_status=job.status,
        )


async def on_image_terminal(session: Any, job: JobRun, outcome: Outcome | None) -> None:
    del outcome  # branch on the final status only
    if job.status == "succeeded":
        return
    if job.entity_type == SIZE_CHART_READ_ENTITY:
        await _size_chart_read_failed(session, job)
        return
    if job.entity_type != CANDIDATE_ENTITY:
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
    if old.entity_type == SIZE_CHART_READ_ENTITY:
        log.info("size_chart_read.requeue_refused", job_run_id=str(old.id))
        raise NotEligibleError("ask for the size-chart read again through the API")
    if old.entity_type != CANDIDATE_ENTITY:
        return
    log.info("image_candidate.requeue_refused", job_run_id=str(old.id))
    raise NotEligibleError("ask for the candidate again through the API")
