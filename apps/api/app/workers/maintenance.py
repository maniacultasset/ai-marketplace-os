"""The daily maintenance run: `python -m app.workers.maintenance` (Amazon DPP 1.7, 2.1).

Prod runs it as the Cloud Run job `aimos-maintenance`, started once a day by Cloud Scheduler
(`scripts/deploy/gcp/deploy.sh`), so retention and the account purge do not wait for the worker
(`WORKER_ENABLED=false` until the AI package lands). It reuses the worker's own periodic steps,
in this order, each in its own transactions:

1. `prune_oauth_states`: expired OAuth state and pending rows (sealed pending tokens included).
2. `prune_idempotency_keys`: expired HTTP idempotency keys.
   `prune_login_flows` (ADR-0020): expired Google sign-in flows, and a warning when more than
   200 sign-up requests are pending.
3. `retention_daily`: import rows, imports and job chains past their cutoffs, old refresh tokens,
   closed invitations, inactive brand chunks, then the image orphan sweep. The queue's own entries
   are the worker's (no queue app here).
4. `purge_disconnected_accounts`: every marketplace account revoked or disconnected for 20 days,
   as `migrator` (ALEMBIC_DATABASE_URL).

Every step is idempotent, so a second run the same day, or a run beside a worker, deletes nothing
twice. A failing step is logged and the next one still runs; the exit code is 1 when any step
failed (Cloud Run then marks the execution failed), else 0. Logs carry counts and ids only.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Final

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.core.observability import init_sentry
from app.core.request_context import new_request_id, request_context
from app.db.session import dispose_engine
from app.workers import periodic

log = get_logger(__name__)

Step = Callable[[], Awaitable[Any]]
STEPS: Final[tuple[tuple[str, Step], ...]] = (
    ("oauth_states", periodic.prune_oauth_states),
    ("idempotency_keys", periodic.prune_idempotency_keys),
    ("login_flows", periodic.prune_login_flows),
    ("retention", periodic.retention_daily),
    ("account_purge", periodic.purge_disconnected_accounts),
)


async def run(steps: tuple[tuple[str, Step], ...] = STEPS) -> int:
    """Every step in order; 0 when all succeeded, else 1."""
    failed: list[str] = []
    try:
        for name, step in steps:
            try:
                with request_context(new_request_id()):
                    await step()
            except Exception as exc:
                log.exception("maintenance.step_failed", step=name, error_class=type(exc).__name__)
                failed.append(name)
    finally:
        await dispose_engine()
    log.info("maintenance.done", steps=len(steps), failed=",".join(failed))
    return 1 if failed else 0


def main() -> int:
    settings = get_settings()
    configure_logging(settings)
    init_sentry(settings)
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
