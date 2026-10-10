"""Platform reads and calls of the periodic maintenance tasks (S13 A10-A11; DATABASE.md
:1836-1846): the tenant list the per-tenant loops walk (read through `platform_scope()`),
`expire_trials()` (0004, SECURITY DEFINER, EXECUTE granted to app_rw), and the daily retention
deletes, one tenant at a time inside that tenant's `unit_of_work` (RLS applies; each statement
also names the tenant).

Retention (DATABASE.md:1836-1846): `imports` and their `import_rows` after 90 days (children
first; never while the import's job is live); `job_runs` by whole requeue chain (`parent_job_id`), and only when every member is
terminal and past its cutoff (90 days after `finished_at`, 180 for `failed`/`dead_letter`), no
member is a `listing_publish` job (kept for the life of the listing) and no remaining `imports`
or `listing_submissions` row points at a member. Every delete takes at most `BATCH` rows; a
chain's deeper members go first, so a batch boundary never leaves a child pointing at a deleted
parent.

Auth and brand rows (audit DB-3; DATABASE.md retention table): `refresh_tokens` 7 days after
`expires_at` (deleting a successor first only clears its predecessor's `replaced_by`, the FK's
`ON DELETE SET NULL (replaced_by)`), `invitations` 30 days after they closed (accepted, revoked,
or else expired), and inactive `brand_chunks` 30 days after they were deactivated (`updated_at`,
stamped by the trigger when `is_active` went false).
"""

from __future__ import annotations

import uuid
from typing import Any, Final

from sqlalchemy import select, text

from app.db.models.tenancy import Tenant
from app.db.session import get_engine

_EXPIRE_TRIALS: Final = text("SELECT public.expire_trials()")
BATCH: Final = 1000
IMPORT_DAYS: Final = 90
JOB_DAYS: Final = 90
FAILED_JOB_DAYS: Final = 180
REFRESH_TOKEN_DAYS: Final = 7
INVITATION_DAYS: Final = 30
BRAND_CHUNK_DAYS: Final = 30
# `prune_tenant`'s keys, in run order (the daily totals start from these)
PRUNED_TABLES: Final = (
    "import_rows",
    "imports",
    "job_runs",
    "refresh_tokens",
    "invitations",
    "brand_chunks",
)

_IMPORT_ROWS: Final = text(
    "DELETE FROM import_rows WHERE id IN ("
    " SELECT r.id FROM import_rows r JOIN imports i ON i.id = r.import_id"
    " WHERE r.tenant_id = :t AND i.tenant_id = :t"
    " AND i.created_at < now() - make_interval(days => :days)"
    " AND NOT EXISTS (SELECT 1 FROM job_runs j WHERE j.tenant_id = :t AND j.id = i.job_id"
    "  AND j.status IN ('queued', 'running')) LIMIT :batch)"
)
_IMPORTS: Final = text(
    "DELETE FROM imports WHERE id IN ("
    " SELECT i.id FROM imports i WHERE i.tenant_id = :t"
    " AND i.created_at < now() - make_interval(days => :days)"
    " AND NOT EXISTS (SELECT 1 FROM import_rows r WHERE r.import_id = i.id)"
    " AND NOT EXISTS (SELECT 1 FROM job_runs j WHERE j.tenant_id = :t AND j.id = i.job_id"
    "  AND j.status IN ('queued', 'running')) LIMIT :batch)"
)
_REFRESH_TOKENS: Final = text(
    "DELETE FROM refresh_tokens WHERE tenant_id = :t AND id IN ("
    " SELECT id FROM refresh_tokens WHERE tenant_id = :t"
    " AND expires_at < now() - make_interval(days => :days) LIMIT :batch)"
)
_INVITATIONS: Final = text(
    "DELETE FROM invitations WHERE tenant_id = :t AND id IN ("
    " SELECT id FROM invitations WHERE tenant_id = :t"
    " AND coalesce(accepted_at, revoked_at, expires_at) < now() - make_interval(days => :days)"
    " LIMIT :batch)"
)
_BRAND_CHUNKS: Final = text(
    "DELETE FROM brand_chunks WHERE tenant_id = :t AND id IN ("
    " SELECT id FROM brand_chunks WHERE tenant_id = :t AND NOT is_active"
    " AND updated_at < now() - make_interval(days => :days) LIMIT :batch)"
)
_JOB_CHAINS: Final = text("""
    WITH RECURSIVE chain AS (
      SELECT j.id, j.id AS root, 0 AS depth FROM job_runs j
       WHERE j.tenant_id = :t AND j.parent_job_id IS NULL
      UNION ALL
      SELECT c.id, chain.root, chain.depth + 1 FROM job_runs c
        JOIN chain ON c.parent_job_id = chain.id
       WHERE c.tenant_id = :t
    ),
    kept AS (
      SELECT DISTINCT chain.root FROM chain JOIN job_runs j ON j.id = chain.id
       WHERE j.status NOT IN ('succeeded', 'partial', 'failed', 'cancelled', 'dead_letter')
          OR j.finished_at IS NULL
          OR j.finished_at >= now() - make_interval(days => CASE
               WHEN j.status IN ('failed', 'dead_letter') THEN :failed_days ELSE :days END)
          OR j.kind = 'listing_publish'
          OR EXISTS (SELECT 1 FROM imports i WHERE i.tenant_id = :t AND i.job_id = j.id)
          OR EXISTS (SELECT 1 FROM listing_submissions s
                      WHERE s.tenant_id = :t AND s.job_id = j.id)
    ),
    doomed AS (
      SELECT chain.id FROM chain
       WHERE chain.root NOT IN (SELECT root FROM kept)
       ORDER BY chain.depth DESC, chain.id
       LIMIT :batch
    )
    DELETE FROM job_runs WHERE tenant_id = :t AND id IN (SELECT id FROM doomed)
    """)


async def tenant_ids(session: Any) -> list[uuid.UUID]:
    """Every tenant id, in id order. Cross-tenant: run it only in `platform_scope()`."""
    return list((await session.execute(select(Tenant.id).order_by(Tenant.id))).scalars())


async def expire_trials() -> int:
    """`expire_trials()`: every `trial` past `trial_ends_at` → `suspended`; returns the count."""
    async with get_engine().begin() as connection:
        count = await connection.scalar(_EXPIRE_TRIALS)
    return int(count or 0)


async def _drain(session: Any, stmt: Any, params: dict[str, Any]) -> tuple[int, int]:
    """Run a batched DELETE until it deletes fewer than a batch: (rows, batches)."""
    rows = batches = 0
    while True:
        result = await session.execute(stmt, {**params, "batch": BATCH})
        deleted = int(getattr(result, "rowcount", 0) or 0)
        rows += deleted
        batches += 1
        if deleted < BATCH:
            return rows, batches


async def prune_tenant(session: Any, tenant_id: uuid.UUID) -> dict[str, tuple[int, int]]:
    """One tenant's retention pass, children first: import_rows, imports, then job chains; then
    expired refresh tokens, closed invitations and inactive brand chunks (DB-3). Returns
    `{table: (rows deleted, batches run)}`."""
    base = {"t": tenant_id, "days": IMPORT_DAYS}
    out = {
        "import_rows": await _drain(session, _IMPORT_ROWS, base),
        "imports": await _drain(session, _IMPORTS, base),
        "job_runs": await _drain(
            session,
            _JOB_CHAINS,
            {"t": tenant_id, "days": JOB_DAYS, "failed_days": FAILED_JOB_DAYS},
        ),
        "refresh_tokens": await _drain(
            session, _REFRESH_TOKENS, {"t": tenant_id, "days": REFRESH_TOKEN_DAYS}
        ),
        "invitations": await _drain(
            session, _INVITATIONS, {"t": tenant_id, "days": INVITATION_DAYS}
        ),
        "brand_chunks": await _drain(
            session, _BRAND_CHUNKS, {"t": tenant_id, "days": BRAND_CHUNK_DAYS}
        ),
    }
    return out
