"""Product barcodes over the API (SI5, PLAN §2.4, D12-D14; CODE-PLAN T10, T11).

One dictionary key, `identifier`, with three typed columns (`identifier_type`, `identifier_value`,
`identifier_status`), written with `FactWriter.set_typed_group` and cleared with
`clear_typed(..., "identifier")`, with the seller column provenance. Owner levels are `variant`
and `standalone`: a style never holds one.

- `plan(level, row, ...)` (pure): the target state of the row from the body fields sent, or None
  when nothing changes. A barcode on a style is `FACT_LEVEL_INVALID`; a bad check digit or a
  wrong length for the type is `VALIDATION_FAILED` at `product_identifier.value`.
  - a barcode: `provided` with its type and value;
  - `product_identifier: null`: cleared only when the stored status is `provided`;
  - `identifier_exemption: true`: `exempt` (type and value cleared);
  - `identifier_exemption: false`: cleared only when the stored status is `exempt`.
- `apply(facts, row_id, change, fact)`: the one write.
- `fan_out(...)` (T11): a style's exemption is set on (or cleared from) its live variants.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Final

from mkos_schemas.codes import ErrorCode

from app.core import identifiers
from app.core.errors import ConflictError, InputError

__all__ = [
    "IDENTIFIER_KEY",
    "IdentifierChange",
    "apply",
    "duplicate",
    "is_duplicate",
    "plan",
    "validate",
]

IDENTIFIER_KEY: Final = "identifier"
VALUE_FIELD: Final = "product_identifier.value"
FIELDS: Final = frozenset({"product_identifier", "identifier_exemption"})


@dataclass(frozen=True, slots=True)
class IdentifierChange:
    """The target `(type, value, status)`; `status` None means clear the key."""

    id_type: str | None
    value: str | None
    status: str | None

    @property
    def clears(self) -> bool:
        return self.status is None


def validate(identifier: tuple[str, str]) -> None:
    id_type, value = identifier
    if identifiers.check(id_type, value) is not None:
        raise InputError(ErrorCode.VALIDATION_FAILED, VALUE_FIELD)


def _stored(row: Any) -> tuple[str | None, str | None, str | None]:
    return (
        getattr(row, "identifier_type", None),
        getattr(row, "identifier_value", None),
        getattr(row, "identifier_status", None),
    )


def plan(
    level: str,
    row: Any,
    *,
    identifier: tuple[str, str] | None,
    exemption: bool | None,
    sent: frozenset[str] | set[str],
) -> IdentifierChange | None:
    """The change of `row` (None when the body leaves it as stored). `row` None is a new row."""
    given = "product_identifier" in sent and identifier is not None
    if given and exemption is True:
        raise InputError(ErrorCode.VALIDATION_FAILED, "identifier_exemption")
    if level == "style":
        if given:
            raise InputError(ErrorCode.FACT_LEVEL_INVALID, "product_identifier")
        return None  # a style's own row holds none; its exemption fans out (T11)
    stored = _stored(row) if row is not None else (None, None, None)
    target = stored
    if given:
        assert identifier is not None  # noqa: S101 - narrowed by `given`
        validate(identifier)
        target = (identifier[0], identifier[1], "provided")
    elif "product_identifier" in sent and target[2] == "provided":
        target = (None, None, None)
    if "identifier_exemption" in sent:
        if exemption is True:
            target = (None, None, "exempt")
        elif exemption is False and target[2] == "exempt":
            target = (None, None, None)
    if target == stored:
        return None
    return IdentifierChange(*target)


async def apply(facts: Any, row_id: uuid.UUID, change: IdentifierChange, fact: Any) -> bool:
    """Write `change` to `row_id` through the FactWriter; True when a row was written."""
    if change.clears:
        return bool(await facts.clear_typed(row_id, IDENTIFIER_KEY))
    return bool(
        await facts.set_typed_group(
            row_id,
            {
                "identifier_type": change.id_type,
                "identifier_value": change.value,
                "identifier_status": change.status,
            },
            fact,
        )
    )


UNIQUE: Final = "products_identifier_uq"


def is_duplicate(exc: BaseException) -> bool:
    """A write refused by `products_identifier_uq` (a concurrent product took the barcode): the
    raw driver error inside the block, or the commit's `ConflictError` naming the index."""
    if isinstance(exc, ConflictError):
        return exc.constraint == UNIQUE
    orig = getattr(exc, "orig", None)
    diag = getattr(orig, "diag", None)
    return getattr(diag, "constraint_name", None) == UNIQUE


def duplicate() -> InputError:
    return InputError(ErrorCode.VALIDATION_FAILED, VALUE_FIELD)
