"""Worker settings as code constants (S13 PLAN D6; ADR-0001:32, :44; spike REPORT §5 item 6), not
environment variables: one process runs the `default` and `publish` queues, each with its own
concurrency; SIGTERM gives running jobs `GRACE_S` (Cloud Run allows 10 s) before they are aborted
and released uncounted; heartbeats every `HEARTBEAT_S`; a fallback poll every `POLL_S` besides
LISTEN/NOTIFY (which also sets retry-backoff resolution); a worker silent for `STALLED_WORKER_S`
is pruned. Connections per worker process (ADR-0001:44): the app pool (DATABASE_POOL_SIZE +
DATABASE_MAX_OVERFLOW = 10) plus the procrastinate pool (5) plus one LISTEN connection per queue.
"""

from __future__ import annotations

from typing import Final

from app.db.queue import QUEUE_POOL_MAX_SIZE

DEFAULT_CONCURRENCY: Final = 4
PUBLISH_CONCURRENCY: Final = 2
GRACE_S: Final = 8
HEARTBEAT_S: Final = 10
POLL_S: Final = 2
STALLED_WORKER_S: Final = 30
APP_POOL_MAX: Final = 10
QUEUE_POOL_MAX: Final = QUEUE_POOL_MAX_SIZE
WORKER_SERVICE_NAME: Final = "marketplace-os-worker"
# queue -> its concurrency, in start order
QUEUE_CONCURRENCY: Final = (("default", DEFAULT_CONCURRENCY), ("publish", PUBLISH_CONCURRENCY))
