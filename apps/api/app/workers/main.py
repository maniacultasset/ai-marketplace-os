"""The worker process (`APP_ROLE=worker`; `python -m app.workers.main`).

One process, two procrastinate workers (queue `default` and queue `publish`, each with its own
concurrency, ADR-0001:32) on one queue pool, LISTEN/NOTIFY on. No health server and no port
(a Cloud Run worker pool needs none, CANON.md row 25; liveness is the heartbeat plus the
reaper). Logging and Sentry start exactly as the API's do (S11: locals off, no PII, the
redacting `before_send`). SIGTERM or SIGINT stops both workers: running jobs get `GRACE_S`, are
then aborted and go back to `queued` uncounted, the worker rows unregister, and the process
exits 0. Any other way out (a worker crashing) exits 1.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys

from opentelemetry import trace

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.core.observability import init_sentry, init_tracing, tracer_from
from app.db.queue import get_queue_app
from app.db.session import dispose_engine
from app.workers.periodic import register_periodic
from app.workers.settings import (
    GRACE_S,
    HEARTBEAT_S,
    POLL_S,
    QUEUE_CONCURRENCY,
    STALLED_WORKER_S,
)
from app.workers.tasks import register_tasks

log = get_logger(__name__)
EXIT_OK = 0
EXIT_FAILED = 1


async def run(stop: asyncio.Event | None = None) -> int:
    settings = get_settings()
    configure_logging(settings)
    init_sentry(settings)
    # S18 T12: the pipeline's node spans use the worker's own provider; S23 C1: the worker
    # (never the API) also makes it the OTel global provider, so `trace.get_tracer()` in task
    # code nests under the job span
    from app.services.pipeline_deps import use_tracer

    provider = init_tracing(settings)
    use_tracer(tracer_from(provider))
    trace.set_tracer_provider(provider)
    stop = stop if stop is not None else asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    app = get_queue_app()
    register_tasks(app)
    register_periodic(app)
    try:
        async with app.open_async():
            workers = [
                asyncio.create_task(
                    app.run_worker_async(
                        queues=[queue],
                        name=f"worker-{queue}",
                        concurrency=concurrency,
                        wait=True,
                        listen_notify=True,
                        install_signal_handlers=False,
                        shutdown_graceful_timeout=GRACE_S,
                        update_heartbeat_interval=HEARTBEAT_S,
                        stalled_worker_timeout=STALLED_WORKER_S,
                        fetch_job_polling_interval=POLL_S,
                        abort_job_polling_interval=POLL_S,
                    ),
                    name=f"worker-{queue}",
                )
                for queue, concurrency in QUEUE_CONCURRENCY
            ]
            stopping = asyncio.create_task(stop.wait(), name="stop")
            log.info("worker.started", queues=[q for q, _ in QUEUE_CONCURRENCY])
            await asyncio.wait([*workers, stopping], return_when=asyncio.FIRST_COMPLETED)
            clean = stopping.done()
            for task in workers:
                task.cancel()
            results = await asyncio.gather(*workers, return_exceptions=True)
            stopping.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stopping
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)
        await dispose_engine()
    failed = [
        type(r).__name__
        for r in results
        if isinstance(r, BaseException) and not isinstance(r, asyncio.CancelledError)
    ]
    if failed or not clean:
        log.error("worker.stopped_unexpectedly", errors=failed)
        return EXIT_FAILED
    log.info("worker.stopped")
    return EXIT_OK


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
