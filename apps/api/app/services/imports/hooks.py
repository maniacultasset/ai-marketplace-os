"""The `bulk_import` job hooks (S13 B9, A14, A15; the `Hook`/`RequeueHook` shapes of
`app.services.job_hooks`).

- `import_terminal_hook(session, job, outcome)`: in the job's terminal transaction, the job's
  final status is mirrored onto its import (`dead_letter` → `failed`) with the totals counted
  from `import_rows`, and a failed job's report is kept on the import. Fenced: it writes only
  while this job still owns the import (`imports.job_id = job.id`).
- `import_requeue_hook(session, old, new)`: a staff requeue hands the import to the new job
  (`job_id` and `status='queued'`), only while the old job still owns it.
"""

from __future__ import annotations

from typing import Any

from app.db.models.ops import JobRun
from app.repositories.imports import ImportRepository, ImportRowRepository
from app.workers.registry import Outcome

FAILED_STATES = frozenset({"failed", "dead_letter"})


async def import_terminal_hook(session: Any, job: JobRun, outcome: Outcome | None) -> None:
    if job.entity_type != "import":
        return
    del outcome  # the status is final on `job`, whatever the path
    totals = await ImportRowRepository(session, job.tenant_id).totals(job.entity_id)
    await ImportRepository(session, job.tenant_id).finish(
        job.entity_id,
        job.id,
        status=job.status,
        totals=totals,
        error_report_json=job.error_report_json if job.status in FAILED_STATES else None,
    )


async def import_requeue_hook(session: Any, old: JobRun, new: JobRun) -> None:
    if old.entity_type != "import":
        return
    await ImportRepository(session, old.tenant_id).hand_to_job(old.entity_id, old.id, new.id)
