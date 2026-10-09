"""The explicit database-error maps (KNOWN-ISSUES.md:50): a constraint name, or a guard's own
SQLSTATE, decides the `ErrorCode`, never the driver message, which can echo row values.

- `to_conflict`: an IntegrityError (class 23) by constraint name.
- `map_db_error`: the SQLSTATEs the revision 0004_integrity guards raise (DATABASE.md; the
  unnamed refusals all raise GRD01). `LST02` carries CONSTRAINT 'paused' or 'archived'. A standard Postgres SQLSTATE (digit-led
  class) is not a guard's and maps to None; an unknown letter-led one is INTERNAL_ERROR."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from mkos_schemas.codes import ErrorCode
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.core.errors import ConflictError, DomainError, InputError

logger = logging.getLogger(__name__)

CONSTRAINT_CODES: Final[Mapping[str, ErrorCode]] = MappingProxyType(
    {
        "products_tenant_sku_idx": ErrorCode.SKU_CONFLICT,  # DATABASE.md:407
        "ux_ls_open": ErrorCode.SUBMISSION_IN_FLIGHT,  # one open submission per listing (:872)
        "ux_job_runs_live": ErrorCode.LISTING_BUSY,  # one live job per (listing, kind) (:949)
    }
)
GUARD_SQLSTATES: Final[Mapping[str, ErrorCode]] = MappingProxyType(
    {
        "MKC01": ErrorCode.MAKER_CHECKER_VIOLATION,  # lv_guard (DATABASE.md:788)
        "OWN01": ErrorCode.LAST_OWNER_REQUIRED,  # last_owner_guard (:256)
        "LST01": ErrorCode.INVALID_STATUS_TRANSITION,  # listings_status_guard (:1402)
        "LST02": ErrorCode.LISTING_PAUSED,  # by constraint: LST02_CONSTRAINTS (:767)
        "REV01": ErrorCode.BULK_APPROVE_REQUIRES_VALIDATED,  # review backstop (:804)
        "SUB01": ErrorCode.DUPLICATE_SUBMISSION_BLOCKED,  # ls_guard (:875)
        "SUB02": ErrorCode.SUBMISSION_ATTEMPTS_EXHAUSTED,  # ls_guard (:874)
        "SUB03": ErrorCode.INTERNAL_ERROR,  # ls_guard outcome ledger (:876): a programming error
        "SUB04": ErrorCode.FACTS_CHANGED_SINCE_APPROVAL,  # ls_guard (:874)
        "LIT01": ErrorCode.SELLER_SKU_CONFLICT,  # li_guard (:847)
        "VAR01": ErrorCode.VARIATION_INVALID,  # variant_matrix_guard (:452)
        "VAR02": ErrorCode.VARIANT_MATRIX_INCOMPLETE,  # the deferred matrix check (:452)
        "GRD01": ErrorCode.INTERNAL_ERROR,  # every other guard refusal
    }
)
LST02_CONSTRAINTS: Final[Mapping[str, ErrorCode]] = MappingProxyType(
    {"paused": ErrorCode.LISTING_PAUSED, "archived": ErrorCode.LISTING_ARCHIVED}
)
_NAME: Final = re.compile(r"[A-Za-z0-9_]{1,63}")
_SQLSTATE: Final = re.compile(r"[0-9A-Z]{5}")


def _constraint_name(exc: DBAPIError) -> str | None:
    diag = getattr(exc.orig, "diag", None)
    name = getattr(diag, "constraint_name", None)
    return name if isinstance(name, str) and _NAME.fullmatch(name) else None


def to_conflict(exc: IntegrityError) -> ConflictError:
    name = _constraint_name(exc)
    code = CONSTRAINT_CODES.get(name) if name else None
    if code is None:
        sqlstate = getattr(exc.orig, "sqlstate", None)
        safe_state = (
            sqlstate if isinstance(sqlstate, str) and _SQLSTATE.fullmatch(sqlstate) else "?"
        )
        logger.error(
            "unmapped integrity error: constraint=%s sqlstate=%s", name or "<none>", safe_state
        )
        return ConflictError(ErrorCode.INTERNAL_ERROR, name or "<none>")
    return ConflictError(code, name or "<none>")


# The 0007 facts guard (DATABASE.md:1182-1185): a refused fact is the writer's 422, not a 409.
FACT_SQLSTATES: Final[Mapping[str, ErrorCode]] = MappingProxyType(
    {
        "PRV01": ErrorCode.FACT_SOURCE_FORBIDDEN,
        "PRV02": ErrorCode.UNKNOWN_FACT_KEY,
        "PRV03": ErrorCode.FACT_LEVEL_INVALID,
    }
)


def map_db_error(exc: DBAPIError) -> DomainError | None:
    """The `DomainError` for a 0004 guard's refusal, by SQLSTATE (LST02 also by constraint);
    None for a standard Postgres SQLSTATE (not a guard's) or a driver error without one."""
    sqlstate = getattr(exc.orig, "sqlstate", None)
    if not isinstance(sqlstate, str) or not _SQLSTATE.fullmatch(sqlstate) or sqlstate[0].isdigit():
        return None
    if sqlstate in FACT_SQLSTATES:
        return InputError(FACT_SQLSTATES[sqlstate], "attributes")
    name = _constraint_name(exc)
    code = GUARD_SQLSTATES.get(sqlstate)
    if sqlstate == "LST02":
        code = LST02_CONSTRAINTS.get(name or "", ErrorCode.INTERNAL_ERROR)
    if code is None:
        logger.error("unmapped guard error: sqlstate=%s", sqlstate)
        code = ErrorCode.INTERNAL_ERROR
    return ConflictError(code, name or sqlstate)
