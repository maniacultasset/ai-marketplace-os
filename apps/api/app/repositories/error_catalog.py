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
