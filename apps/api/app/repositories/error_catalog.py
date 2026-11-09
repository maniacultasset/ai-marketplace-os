"""The marketplace error catalogue (SI11 T19; ADR-0028 proposed; P:A5, D12-D13): one row per
(marketplace, code, error code, class) the marketplaces answered, from the
`marketplace_error_catalog` view (revision 0022, `app_platform_ro` only). Counts and dates only:
the view holds no tenant id and no message text. Cross-tenant: run it only in `platform_scope()`
(the staff CLI) or the admin's platform session (the ops page); this module opens neither."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import text

MAX_ROWS: Final = 500
_COLUMNS: Final = (
    "marketplace, marketplace_code, error_code, error_class, mapped, issues, occurrences, "
    "tenants, first_seen, last_seen, preview, resolutions, recurrence_rate"
)


@dataclass(frozen=True, slots=True)
class CatalogStat:
    marketplace: str
    marketplace_code: str | None
    error_code: str
    error_class: str
    mapped: bool
    issues: int
    occurrences: int
    tenants: int
    first_seen: datetime
    last_seen: datetime
    preview: bool
    resolutions: dict[str, int]
    recurrence_rate: Decimal


async def catalog_rows(
    session: Any, *, unmapped: bool = False, limit: int = 200
) -> list[CatalogStat]:
    """The catalogue, most issues first; `unmapped` keeps only the codes no map row matched."""
    if isinstance(limit, bool) or not 1 <= limit <= MAX_ROWS:
        raise ValueError(f"limit must be in 1..{MAX_ROWS}")
    where = "WHERE NOT mapped " if unmapped else ""
    stmt = text(
        f"SELECT {_COLUMNS} FROM public.marketplace_error_catalog {where}"  # noqa: S608 - fixed text
        "ORDER BY issues DESC, marketplace, marketplace_code NULLS LAST, error_code, error_class "
        "LIMIT :limit"
    )
    rows = (await session.execute(stmt, {"limit": limit})).all()
    return [
        CatalogStat(
            marketplace=r.marketplace,
            marketplace_code=r.marketplace_code,
            error_code=r.error_code,
            error_class=r.error_class,
            mapped=bool(r.mapped),
            issues=int(r.issues),
            occurrences=int(r.occurrences or 0),
            tenants=int(r.tenants),
            first_seen=r.first_seen,
            last_seen=r.last_seen,
            preview=bool(r.preview),
            resolutions={str(k): int(v) for k, v in (r.resolutions or {}).items()},
            recurrence_rate=Decimal(str(r.recurrence_rate or 0)),
        )
        for r in rows
    ]


# ---- SI13 T13: the diagnosis scheduler's reads (ids and codes only) ----

_UNMAPPED: Final = text("""
    SELECT c.marketplace, c.marketplace_code
    FROM public.marketplace_error_catalog c
    WHERE NOT c.mapped
      AND c.error_class = 'unknown'
      AND c.marketplace_code IS NOT NULL
      AND c.marketplace_code ~ '^[0-9A-Za-z_.:-]{1,128}$'
      AND NOT EXISTS (
        SELECT 1 FROM public.marketplace_error_proposals p
        WHERE p.marketplace = c.marketplace AND p.match_on = 'code'
          AND p.raw_value = c.marketplace_code
          AND NOT (p.status = 'failed' AND p.tries < 3
                   AND (p.next_try_at IS NULL OR p.next_try_at <= now())))
    GROUP BY c.marketplace, c.marketplace_code
    ORDER BY max(c.last_seen) DESC, c.marketplace, c.marketplace_code
    LIMIT :limit
    """)
_NEWEST: Final = text(
    "SELECT id, tenant_id FROM public.listing_issues "
    "WHERE marketplace = :marketplace AND marketplace_code = :code AND error_class = 'unknown' "
    "ORDER BY last_seen_at DESC, id LIMIT 1"
)


async def unmapped_codes(session: Any, *, limit: int) -> list[tuple[str, str]]:
    """(marketplace, code) of the unmapped `unknown` codes that have no proposal, or only a
    retryable failed one, newest first. Synthetic pre-check codes never qualify (their class is
    not `unknown`)."""
    if isinstance(limit, bool) or not 1 <= limit <= MAX_ROWS:
        raise ValueError(f"limit must be in 1..{MAX_ROWS}")
    rows = (await session.execute(_UNMAPPED, {"limit": limit})).all()
    return [(str(r.marketplace), str(r.marketplace_code)) for r in rows]


async def newest_sample(session: Any, marketplace: str, code: str) -> tuple[Any, Any] | None:
    """(issue id, tenant id) of the newest issue with this code; ids only."""
    row = (await session.execute(_NEWEST, {"marketplace": marketplace, "code": code})).first()
    return None if row is None else (row.id, row.tenant_id)


# ---- SI13 T16: the export's reads ----

_UNMAPPED_COUNT: Final = text(
    "SELECT count(DISTINCT (marketplace, marketplace_code)) FROM public.marketplace_error_catalog "
    "WHERE NOT mapped AND marketplace_code IS NOT NULL "
    "AND (CAST(:m AS text) IS NULL OR marketplace = :m)"
)


async def unmapped_count(session: Any, marketplace: str | None) -> int:
    """How many distinct marketplace codes no map row matched yet."""
    return int((await session.execute(_UNMAPPED_COUNT, {"m": marketplace})).scalar_one() or 0)


async def map_rows(session: Any, marketplace: str | None) -> list[Any]:
    """Every error-map row (enabled or not) of the marketplace, for the export."""
    from sqlalchemy import select

    from app.db.models.platform import MarketplaceErrorMap as M

    stmt = select(M).order_by(M.marketplace, M.match_on, M.raw_value, M.version)
    if marketplace is not None:
        stmt = stmt.where(M.marketplace == marketplace)
    return list((await session.execute(stmt)).scalars())


# ---- SI13 T15/T17: the value-map report's platform reads ----

_TENANTS: Final = text("SELECT id FROM public.tenants ORDER BY id LIMIT :limit")
_ENUM_SPECS: Final = text("SELECT attributes FROM public.category_templates WHERE is_current")


async def value_map_inputs(
    session: Any, *, tenants: int
) -> tuple[list[Any], dict[str, set[str]], set[str]]:
    """(tenant ids, enum values per attribute key, the enum keys) from the current templates."""
    ids = list((await session.execute(_TENANTS, {"limit": tenants})).scalars())
    enums: dict[str, set[str]] = {}
    keys: set[str] = set()
    for (attributes,) in await session.execute(_ENUM_SPECS):
        for key, spec in ((attributes or {}).get("specs") or {}).items():
            if not isinstance(spec, dict) or spec.get("value_type") != "enum":
                continue
            keys.add(str(key))
            values = list(spec.get("allowed_values") or []) + list(
                (spec.get("value_map") or {}).values()
            )
            enums.setdefault(str(key), set()).update(str(v) for v in values)
    return ids, enums, keys
