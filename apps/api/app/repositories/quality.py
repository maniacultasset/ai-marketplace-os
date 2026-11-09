"""The quality dashboard reads (SI14, BE-13, AC10): first-pass, blocked and edit rates and the
model cost per generated listing, for one tenant (`QualityRepository`) or across tenants
(`platform_quality`, ids and counts only), over a UTC range `[lo, hi)`.

Definitions (CODE-PLAN T19; PLAN D10 as restated by Q-c):
- generated: distinct listings with a version of origin `generation` or `regeneration` created in
  range, per marketplace;
- approved: versions approved in range. For each, the chain walks `parent_version_id` up to the
  nearest generation (depth <= 50): it is first-pass when it is itself a generation with
  `attempts_used <= 1`, edited when its chain holds an `edit` version, and its edit versions'
  `changed_paths` count toward the edited fields (top 20);
- blocked: generated listings that reached `blocked` in range, through the generation worker
  (`job_runs.result_json.status`, kept 90 days) or a user move (`audit_log.after.status`), once each;
- cost: the `agent_runs` of the generated listings started in range. Classify and column-map runs
  have no listing and are not counted.

One SQL text, two tenant clauses: the tenant read names `tenant_id = :t` on every table (RLS is the
backstop) through the repository's own session; the platform read runs on a session the caller
opens (`platform_scope()`, allowed only in `app/admin/**`, CANON.md:78), never here.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.repositories.analytics import AnalyticsRepository, _budget_error

MAX_EDITED_FIELDS: Final = 20
MAX_CHAIN_DEPTH: Final = 50
_TENANT: Final = "{a}.tenant_id = CAST(:t AS uuid)"
_PLATFORM: Final = "(CAST(:t AS uuid) IS NULL OR {a}.tenant_id = CAST(:t AS uuid))"


def _sql(scope: str) -> tuple[str, str]:
    """The (counts, edited fields) SQL with `scope` naming the tenant clause per alias."""

    def on(alias: str) -> str:
        return scope.format(a=alias)

    base = f"""
WITH RECURSIVE gen AS (
  SELECT DISTINCT v.tenant_id, v.listing_id, l.marketplace
    FROM listing_versions v
    JOIN listings l ON l.tenant_id = v.tenant_id AND l.id = v.listing_id
   WHERE {on("v")} AND {on("l")}
     AND v.origin IN ('generation', 'regeneration')
     AND v.created_at >= :lo AND v.created_at < :hi
),
approved AS (
  SELECT v.tenant_id, v.id, v.listing_id, v.origin, v.attempts_used, l.marketplace
    FROM listing_versions v
    JOIN listings l ON l.tenant_id = v.tenant_id AND l.id = v.listing_id
   WHERE {on("v")} AND {on("l")}
     AND v.approved_at >= :lo AND v.approved_at < :hi
),
chain AS (
  SELECT a.tenant_id, a.id AS root, v.id, v.origin, v.parent_version_id, v.changed_paths, 0 AS depth
    FROM approved a
    JOIN listing_versions v ON v.tenant_id = a.tenant_id AND v.id = a.id
  UNION ALL
  SELECT c.tenant_id, c.root, p.id, p.origin, p.parent_version_id, p.changed_paths, c.depth + 1
    FROM chain c
    JOIN listing_versions p ON p.tenant_id = c.tenant_id AND p.id = c.parent_version_id
   WHERE c.origin NOT IN ('generation', 'regeneration') AND c.depth < {MAX_CHAIN_DEPTH}
     AND {on("p")}
),
blocked AS (
  SELECT DISTINCT g.tenant_id, g.listing_id
    FROM gen g
   WHERE EXISTS (
           SELECT 1 FROM job_runs j
            WHERE j.tenant_id = g.tenant_id AND j.entity_type = 'listing'
              AND j.entity_id = g.listing_id AND j.result_json ->> 'status' = 'blocked'
              AND j.finished_at >= :lo AND j.finished_at < :hi)
      OR EXISTS (
           SELECT 1 FROM audit_log al
            WHERE al.tenant_id = g.tenant_id AND al.entity_type = 'listing'
              AND al.entity_id = g.listing_id AND al.after ->> 'status' = 'blocked'
              AND al.occurred_at >= :lo AND al.occurred_at < :hi)
)"""  # noqa: S608 - fixed identifiers and the scope clause only; values are binds
    counts = base + """
SELECT k.tenant_id, k.marketplace,
       (SELECT count(*) FROM gen g WHERE g.tenant_id = k.tenant_id AND g.marketplace = k.marketplace)
         AS generated,
       (SELECT count(*) FROM approved a WHERE a.tenant_id = k.tenant_id
          AND a.marketplace = k.marketplace) AS approved,
       (SELECT count(*) FROM approved a WHERE a.tenant_id = k.tenant_id
          AND a.marketplace = k.marketplace AND a.origin IN ('generation', 'regeneration')
          AND coalesce(a.attempts_used, 1) <= 1) AS first_pass,
       (SELECT count(*) FROM approved a WHERE a.tenant_id = k.tenant_id
          AND a.marketplace = k.marketplace
          AND EXISTS (SELECT 1 FROM chain c WHERE c.root = a.id AND c.origin = 'edit'))
         AS edited,
       (SELECT count(*) FROM blocked b JOIN gen g ON g.tenant_id = b.tenant_id
          AND g.listing_id = b.listing_id
         WHERE g.tenant_id = k.tenant_id AND g.marketplace = k.marketplace) AS blocked,
       (SELECT coalesce(sum(r.cost_usd), 0) FROM agent_runs r
          JOIN gen g ON g.tenant_id = r.tenant_id AND g.listing_id = r.listing_id
         WHERE g.tenant_id = k.tenant_id AND g.marketplace = k.marketplace
           AND r.started_at >= :lo AND r.started_at < :hi) AS cost
  FROM (SELECT tenant_id, marketplace FROM gen
        UNION SELECT tenant_id, marketplace FROM approved) k
 ORDER BY k.tenant_id, k.marketplace"""  # noqa: S608
    fields = base + f"""
SELECT path, count(*) AS n
  FROM (SELECT unnest(c.changed_paths) AS path FROM chain c WHERE c.origin = 'edit') e
 GROUP BY path
 ORDER BY n DESC, path
 LIMIT {MAX_EDITED_FIELDS}"""  # noqa: S608
    return counts, fields


TENANT_COUNTS, TENANT_FIELDS = _sql(_TENANT)
PLATFORM_COUNTS, PLATFORM_FIELDS = _sql(_PLATFORM)


@dataclass
class QualityCounts:
    generated: int = 0
    approved: int = 0
    first_pass: int = 0
    edited: int = 0
    blocked: int = 0
    cost: Decimal = Decimal("0")

    def add(self, other: QualityCounts) -> None:
        self.generated += other.generated
        self.approved += other.approved
        self.first_pass += other.first_pass
        self.edited += other.edited
        self.blocked += other.blocked
        self.cost += other.cost


@dataclass
class QualityRead:
    """Counts per (tenant, marketplace), and the most edited fields."""

    rows: dict[tuple[uuid.UUID, str], QualityCounts] = field(default_factory=dict)
    edited_fields: list[tuple[str, int]] = field(default_factory=list)

    def by_marketplace(self) -> dict[str, QualityCounts]:
        out: dict[str, QualityCounts] = defaultdict(QualityCounts)
        for (_tenant, market), counts in self.rows.items():
            out[market].add(counts)
        return dict(out)

    def by_tenant(self) -> dict[uuid.UUID, QualityCounts]:
        out: dict[uuid.UUID, QualityCounts] = defaultdict(QualityCounts)
        for (tenant, _market), counts in self.rows.items():
            out[tenant].add(counts)
        return dict(out)


async def _read(
    session: Any, counts_sql: str, fields_sql: str, params: dict[str, Any]
) -> QualityRead:
    try:
        rows = (await session.execute(text(counts_sql), params)).all()
        fields = (await session.execute(text(fields_sql), params)).all()
    except DBAPIError as exc:
        budget = _budget_error(exc)
        if budget is not None:
            raise budget from None
        raise
    read = QualityRead()
    for tenant, market, generated, approved, first_pass, edited, blocked, cost in rows:
        read.rows[(tenant, str(market))] = QualityCounts(
            int(generated),
            int(approved),
            int(first_pass),
            int(edited),
            int(blocked),
            Decimal(cost),
        )
    read.edited_fields = [(str(path), int(n)) for path, n in fields]
    return read


class QualityRepository(AnalyticsRepository):
    """The tenant read; `bounded()` (READ ONLY, 5 s) comes from the S23 repository."""

    async def quality(self, lo: datetime, hi: datetime) -> QualityRead:
        return await _read(
            self.db, TENANT_COUNTS, TENANT_FIELDS, {"t": str(self.tenant_id), "lo": lo, "hi": hi}
        )


async def platform_quality(
    session: Any, lo: datetime, hi: datetime, tenant_id: uuid.UUID | None
) -> QualityRead:
    """The cross-tenant read on a session the caller opened (`platform_scope()` in
    `app/admin/ops.py`); ids and counts only."""
    params = {"t": None if tenant_id is None else str(tenant_id), "lo": lo, "hi": hi}
    return await _read(session, PLATFORM_COUNTS, PLATFORM_FIELDS, params)
