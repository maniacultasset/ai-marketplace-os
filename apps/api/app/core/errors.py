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
