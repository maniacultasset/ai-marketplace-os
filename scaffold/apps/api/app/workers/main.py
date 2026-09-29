"""Worker entrypoint (APP_ROLE=worker).

A Cloud Run *service* rolls back any revision whose container never binds
$PORT, so the worker cannot be `arq app.workers.settings.WorkerSettings`.
One process, two tasks: the arq consumer and a health server. Deploy with
--no-cpu-throttling --min-instances=1, or a background consumer is starved
between requests.
"""
from __future__ import annotations

import asyncio
import signal

import uvicorn
from arq.worker import create_worker
from fastapi import FastAPI
from redis.asyncio import Redis

from app.core.config import settings
from app.workers.settings import WorkerSettings

health = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@health.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "role": "worker",
            "queue": settings.arq_queue_name, "sha": settings.git_sha}


@health.get("/readyz")
async def readyz() -> dict[str, str]:
    redis = Redis.from_url(settings.redis_url)
    try:
        await redis.ping()
        return {"status": "ready"}
    finally:
        await redis.aclose()


async def main() -> None:
    config = uvicorn.Config(health, host="0.0.0.0", port=settings.port,
                            access_log=False, log_config=None)
    server = uvicorn.Server(config)
    worker = create_worker(WorkerSettings, handle_signals=False)  # type: ignore[arg-type]

    def shutdown(signum: int) -> None:
        server.should_exit = True
        worker.handle_sig(signum)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown, sig)

    tasks = [asyncio.create_task(server.serve(), name="health"),
             asyncio.create_task(worker.async_run(), name="arq")]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    server.should_exit = True
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    for task in done:
        task.result()  # re-raise: a dead consumer must fail the revision, not idle


if __name__ == "__main__":
    asyncio.run(main())
