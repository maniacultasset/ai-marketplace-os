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
