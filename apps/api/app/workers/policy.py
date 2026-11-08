"""The per-kind job policy (ADR-0004 §5-§6; DATABASE.md:937, :944; PLAN Decision 5).

One literal row per `JobKind`: the queue (it matches the `job_runs_set_queue` trigger), the body
timeout, `max_attempts` (generation 1, the rest 3) and whether transport, 5xx and 429 failures are
retried. The lease a claim takes is the timeout plus `LEASE_GRACE_S`. Backoff between transport
retries is 1 s doubling with jitter, capped at 60 s (BACKEND-ARCHITECTURE §8.3).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from mkos_schemas.enums import JobKind

LEASE_GRACE_S: Final = 60
BACKOFF_CAP_S: Final = 60


@dataclass(frozen=True, slots=True)
class JobPolicy:
    kind: JobKind
    queue: str
    timeout_s: int
    max_attempts: int
    retry_transport: bool


def _row(kind: JobKind, queue: str, timeout_s: int, max_attempts: int, retry: bool) -> JobPolicy:
    return JobPolicy(kind, queue, timeout_s, max_attempts, retry)


POLICIES: Final[Mapping[JobKind, JobPolicy]] = MappingProxyType(
    {
        JobKind.LISTING_GENERATE: _row(JobKind.LISTING_GENERATE, "default", 300, 1, False),
        JobKind.LISTING_PUBLISH: _row(JobKind.LISTING_PUBLISH, "publish", 600, 3, True),
        JobKind.LISTING_STATUS_SYNC: _row(JobKind.LISTING_STATUS_SYNC, "publish", 120, 3, True),
        JobKind.BULK_IMPORT: _row(JobKind.BULK_IMPORT, "default", 1800, 3, True),
        JobKind.IMAGE_COMPLIANCE: _row(JobKind.IMAGE_COMPLIANCE, "default", 300, 3, True),
        JobKind.BRAND_EMBED: _row(JobKind.BRAND_EMBED, "default", 300, 3, True),
    }
)


# V1 hardening #8: procrastinate fetches the highest priority first within a queue. A publish
# (always human-approved) shares the `publish` slots with the status syncs, so it jumps any
# queued burst of daily syncs instead of waiting behind them. Every defer (enqueue, the reaper's
# requeue, the orphan re-defer) derives the priority from the kind here.
PUBLISH_PRIORITY: Final = 10
_PRIORITIES: Final[Mapping[JobKind, int]] = MappingProxyType(
    {JobKind.LISTING_PUBLISH: PUBLISH_PRIORITY}
)


def priority_for(kind: JobKind | str) -> int:
    """The procrastinate priority of a queue entry of `kind` (0 unless listed above)."""
    return _PRIORITIES.get(JobKind(kind), 0)


# S17b: image_compliance jobs run under a procrastinate queueing lock. V1 hardening #9: the lock
# is per tenant (`image_compliance:<tenant_id>`), so one brand's upload batch no longer holds up
# every other brand's checks; the per-tenant cap of live image jobs (`LIVE_IMAGE_JOB_CAP`) and
# the per-process child semaphore (`app.services.image_runner`) stay. S26: every defer (enqueue,
# the lease reaper's requeue, the orphan re-defer, the staff requeue) derives the lock from the
# kind and tenant here, so a re-deferred job never runs unlocked beside its tenant's other ones.
IMAGE_LOCK_PREFIX: Final = "image_compliance:"


def image_job_lock(tenant_id: object) -> str:
    """The lock of a tenant's upload image job."""
    if tenant_id is None:
        raise ValueError("an image job's lock needs its tenant")
    return f"{IMAGE_LOCK_PREFIX}{tenant_id}"


# SI7 (design §2.2): a candidate job is an `image_compliance` job about an `image_candidate`. It
# runs under its tenant's own lock, so a slow model call never holds up uploads, and one tenant
# runs one candidate at a time (PLAN D19).
CANDIDATE_ENTITY: Final = "image_candidate"
CANDIDATE_LOCK_PREFIX: Final = "image_candidate:"


def lock_for(
    kind: JobKind | str, entity_type: str | None = None, tenant_id: object | None = None
) -> str | None:
    """The procrastinate lock every queue entry of `kind` carries, or None (unserialised). An
    upload image job takes `image_compliance:<tenant_id>`, a candidate job
    (`entity_type='image_candidate'`) `image_candidate:<tenant_id>`."""
    known = JobKind(kind)
    if known is not JobKind.IMAGE_COMPLIANCE:
        return None
    if entity_type == CANDIDATE_ENTITY:
        if tenant_id is None:
            raise ValueError("a candidate job's lock needs its tenant")
        return f"{CANDIDATE_LOCK_PREFIX}{tenant_id}"
    return image_job_lock(tenant_id)


def lease_s(policy: JobPolicy) -> int:
    """The lease a claim takes: the body timeout plus the grace (DATABASE.md:944)."""
    return policy.timeout_s + LEASE_GRACE_S


def backoff_s(attempt: int, rand: float) -> float:
    """The wait before retry `attempt + 1`: `min(60, 2**(attempt-1))`, scaled into [50%, 100%]
    by `rand` in [0, 1] (the jitter)."""
    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    if not 0.0 <= rand <= 1.0:
        raise ValueError("rand must be in [0, 1]")
    base = min(BACKOFF_CAP_S, 1 << min(attempt - 1, 16))
    return base * (0.5 + rand / 2)
