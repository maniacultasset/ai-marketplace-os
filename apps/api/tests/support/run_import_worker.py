"""A worker process for the import resume tests (S13 B12): the real `app.workers.main.run()`
with the real `bulk_import` body, small batches, and a pause after each batch commit, so a test
can kill the process between two commits.

Run as `python apps/api/tests/support/run_import_worker.py --batch-rows N --pause S` with the
worker env (APP_ROLE=worker, DATABASE_URL as app_rw, S3_ENDPOINT_URL)."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # apps/api

from app.services.imports import runner
from app.workers import main


def _slow(batch_rows: int, pause: float) -> None:
    real = runner._run_batch

    async def batch(run: Any, units: Any) -> None:
        await real(run, units)
        await asyncio.sleep(pause)

    runner.BATCH_ROWS = batch_rows  # type: ignore[misc]
    runner._run_batch = batch  # type: ignore[assignment]


def cli() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-rows", type=int, default=10)
    parser.add_argument("--pause", type=float, default=1.0)
    args = parser.parse_args()
    _slow(args.batch_rows, args.pause)
    return asyncio.run(main.run())


if __name__ == "__main__":
    sys.exit(cli())
