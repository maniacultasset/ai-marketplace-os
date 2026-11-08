"""The error catalogue's staff view (SI11 T19; P:A5, D13), pure: the lane a code takes and, for
an unmapped code, a suggested `marketplace_error_map` row a staff member copies into the seed
file in a PR (never written here; `errors load` loads it on deploy).

`suggested_row(stat)` is deterministic: the code's own error code and class (what the adapter
already mapped it to, from the marketplace's category when one maps, else `unknown`), its
code's retry class, `fix_action` null (SI9 D5), `policy_ref.observed` true and no doc link (the
reviewer adds the public page the row's cause is confirmed on).
"""

from __future__ import annotations

from typing import Any, Final

from mkos_schemas.codes import ERROR_CODE_META, ErrorCode

from app.services.resolution.plan import lane_for

PLAIN_TEXT: Final = "The marketplace refused this listing for a reason Kuinn does not know yet."
SELLER_ACTION: Final = "Kuinn's team is looking at this refusal; no action is needed from you yet."


def _code(value: str) -> ErrorCode:
    try:
        return ErrorCode(value)
    except ValueError:
        return ErrorCode.MARKETPLACE_UNKNOWN_ERROR


def lane_of(stat: Any) -> str:
    source = f"{stat.marketplace}_adapter"
    return lane_for(stat.error_code, stat.error_class, source)


def suggested_row(stat: Any) -> dict[str, Any] | None:
    """The map row to review for an unmapped code with a raw code; None otherwise."""
    if stat.mapped or not stat.marketplace_code:
        return None
    code = _code(stat.error_code)
    return {
        "marketplace": stat.marketplace,
        "match_on": "code",
        "raw_value": str(stat.marketplace_code)[:64],
        "version": 1,
        "error_code": code.value,
        "error_class": stat.error_class,
        "retry_class": ERROR_CODE_META[code].retry_class.value,
        "severity": "error",
        "auto_fixable": False,
        "fix_action": None,
        "field_hint": None,
        "plain_text": PLAIN_TEXT,
        "seller_action": SELLER_ACTION,
        "policy_ref": {"source": "observed", "observed": True},
        "enabled": True,
    }


def catalog_line(stat: Any) -> dict[str, Any]:
    """One catalogue row as JSON-ready data (the CLI line and the ops page row)."""
    return {
        "marketplace": stat.marketplace,
        "marketplace_code": stat.marketplace_code,
        "mapped": stat.mapped,
        "error_code": stat.error_code,
        "error_class": stat.error_class,
        "lane": lane_of(stat),
        "issues": stat.issues,
        "occurrences": stat.occurrences,
        "tenants": stat.tenants,
        "first_seen": stat.first_seen.isoformat(),
        "last_seen": stat.last_seen.isoformat(),
        "preview": stat.preview,
        "recurrence_rate": str(stat.recurrence_rate),
        "resolutions": dict(sorted(stat.resolutions.items())),
        "suggested_row": suggested_row(stat),
    }
