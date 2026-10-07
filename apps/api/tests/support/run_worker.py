"""A worker process for the subprocess tests (S13 A8, A9): the real `app.workers.main.run()`
with one fake body per kind, which sleeps for the job payload's `sleep` seconds (capped by
`--max-sleep`) and then succeeds, and optionally a shorter lease grace for the reaper tests.

Run as `python apps/api/tests/support/run_worker.py [--max-sleep S] [--lease-grace S]` with the
worker env (APP_ROLE=worker, DATABASE_URL as app_rw). The lease grace is overridable here only.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # apps/api

from app.workers import main, registry, wrapper
from app.workers.policy import POLICIES, JobPolicy
from app.workers.registry import JobContext, Outcome


def _install(max_sleep: float) -> None:
    async def body(ctx: JobContext) -> Outcome:
        await asyncio.sleep(min(float(ctx.payload.get("sleep", 0)), max_sleep))
        return Outcome("succeeded", {"pid": os.getpid()})

    for kind in POLICIES:
        registry.register(kind, body)


def _timing(timeout_s: int | None, grace_s: int | None) -> None:
    from app.workers import policy

    if timeout_s is not None:
        wrapper.POLICIES = {  # type: ignore[attr-defined]
            kind: JobPolicy(p.kind, p.queue, timeout_s, p.max_attempts, p.retry_transport)
            for kind, p in POLICIES.items()
        }
    if grace_s is not None:
        policy.LEASE_GRACE_S = grace_s  # type: ignore[misc]


def cli() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-sleep", type=float, default=3600.0)
    parser.add_argument("--timeout", type=int, default=None)
    parser.add_argument("--lease-grace", type=int, default=None)
    args = parser.parse_args()
    _install(args.max_sleep)
    _timing(args.timeout, args.lease_grace)
    return asyncio.run(main.run())


if __name__ == "__main__":
    sys.exit(cli())
