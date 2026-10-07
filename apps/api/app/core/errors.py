"""Domain errors raised below the HTTP layer. S10 maps them onto the one `ErrorReport`.

Messages carry codes, constraint names and counts only, never row values.
"""

from __future__ import annotations

from mkos_schemas.codes import ErrorCode


class DomainError(Exception):
    """The base of every error a service may raise on purpose."""


class ConflictError(DomainError):
    """A database constraint refused the write. `code` is the mapped `ErrorCode`."""

    def __init__(self, code: ErrorCode, constraint: str) -> None:
        super().__init__(f"{code.value} (constraint {constraint})")
        self.code = code
        self.constraint = constraint


class RowCountError(DomainError):
    """A state-changing statement touched a different number of rows than it must
    (DATABASE.md:133)."""

    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(f"expected {expected} row(s), statement touched {actual}")
        self.expected = expected
        self.actual = actual


class NotFoundError(DomainError):
    """The row is missing, or belongs to another tenant (which reads as missing, PLAN D6).
    `field` is a server constant naming what was looked up (`product_id`, `images[0]`), never
    the looked-up value, so a foreign id and a missing one answer the same bytes."""

    def __init__(self, field: str = "resource") -> None:
        super().__init__(field)
        self.field = field


class InputError(DomainError):
    """A request value the service refuses after the schema passed it (422). `field` is the
    body path (`sku`, `attributes.color`, `images[0]`); the value itself is never kept."""

    def __init__(self, code: ErrorCode, field: str) -> None:
        if code is ErrorCode.INTERNAL_ERROR:
            raise ValueError("InputError never carries INTERNAL_ERROR")
        super().__init__(f"{code.value} at {field}")
        self.code = code
        self.field = field


class ForbiddenError(DomainError):
    """The caller's role may not make this change (403 PERMISSION_DENIED)."""


class LimitError(DomainError):
    """A per-tenant cap refused the request (429 `RATE_LIMITED`; the live-import cap, PD13).
    `limit` names the cap (a server constant), never a request value."""

    def __init__(self, limit: str, code: ErrorCode = ErrorCode.RATE_LIMITED) -> None:
        super().__init__(f"{code.value} ({limit})")
        self.code = code
        self.limit = limit
