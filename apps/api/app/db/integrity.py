"""The explicit IntegrityError map (KNOWN-ISSUES.md:50): a constraint name decides the
`ErrorCode`, never the driver message, which can echo row values."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from mkos_schemas.codes import ErrorCode
from sqlalchemy.exc import IntegrityError

from app.core.errors import ConflictError

logger = logging.getLogger(__name__)

CONSTRAINT_CODES: Final[Mapping[str, ErrorCode]] = MappingProxyType(
    {"products_tenant_sku_idx": ErrorCode.SKU_CONFLICT}  # DATABASE.md:407
)
_NAME: Final = re.compile(r"[A-Za-z0-9_]{1,63}")
_SQLSTATE: Final = re.compile(r"[0-9A-Z]{5}")


def _constraint_name(exc: IntegrityError) -> str | None:
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
