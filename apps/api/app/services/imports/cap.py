"""The per-tenant live-import cap (S13 PD13, Q10): at most `LIVE_IMPORT_CAP` `bulk_import` jobs
`queued` or `running` per tenant. `enforce_live_cap(repo)` raises 429 `RATE_LIMITED` past it.
Inside the job-writing transaction the caller takes `lock_tenant_imports()` first, so two
concurrent uploads (or rows retries) of one tenant count one after the other."""

from __future__ import annotations

from typing import Final

from app.core.errors import LimitError
from app.repositories.imports import ImportRepository

LIVE_IMPORT_CAP: Final = 2


async def enforce_live_cap(repo: ImportRepository) -> None:
    if await repo.count_live_bulk_imports() >= LIVE_IMPORT_CAP:
        raise LimitError("live imports per tenant")
