"""The deterministic content fix of a marketplace refusal (SI11 T15; ADR-0028 proposed; P:B5
part 1, D3). The resolution executor runs it to plan (`PlanContext.content_fixable`) and the
auto-fix job runs it again on the base version to make the fix version, so both agree.

- `content_fixes(listing, rows, limit_of)` (pure): for each content or keyword refusal SI4's
  policy routes `deterministic` (today a length refusal), the SI4 trim (`fix_to_limit`) against
  the marketplace's limit; one fix per path, in a fixed order, each on the result of the last.
  A refusal with no limit, or one the trim cannot meet within SI4's share cap, is left alone.
- `prepare(uow, listing, version, rows)`: `content_fixes` on the stored version, the limit from
  the marketplace's adapter (`content_limit`: the stored Product Type Definition, else the limit
  the refusal states).

A content fix never resends: it makes a new version a non-maker approves (the resend rule).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource, Severity
from mkos_schemas.errors import ErrorItem
from mkos_schemas.listing import CanonicalListing

from app.services.auto_fix.fixers import FixRecord, fix_to_limit

CONTENT_ROOTS: Final = ("content.", "keywords.")
LENGTH_CODES: Final = frozenset({ErrorCode.FIELD_TOO_LONG.value})


@dataclass(frozen=True, slots=True)
class ContentFix:
    issue_id: uuid.UUID
    record: FixRecord


def _item(row: Any) -> ErrorItem:
    try:
        source = ErrorSource(row.source)
    except ValueError:
        source = ErrorSource.SYSTEM
    return ErrorItem(
        field=row.field_path,
        error_code=ErrorCode(row.error_code),
        severity=Severity.ERROR,
        message=row.message or "-",
        source=source,
        retryable=False,
        raw_code=row.marketplace_code,
    )


def fixable(row: Any) -> bool:
    return (
        row.severity == "error"
        and row.error_code in LENGTH_CODES
        and str(row.field_path).startswith(CONTENT_ROOTS)
    )


def content_fixes(
    listing: CanonicalListing, rows: Sequence[Any], limit_of: Callable[[Any], int | None]
) -> tuple[CanonicalListing, tuple[ContentFix, ...]]:
    fixes: list[ContentFix] = []
    done: set[str] = set()
    for row in sorted(rows, key=lambda r: (str(r.field_path), str(r.id))):
        if not fixable(row) or row.field_path in done:
            continue
        limit = limit_of(row)
        if limit is None:
            continue
        found = fix_to_limit(listing, _item(row), limit)
        if found is None:
            continue
        listing, record = found
        done.add(row.field_path)
        fixes.append(ContentFix(row.id, record))
    return listing, tuple(fixes)


async def prepare(
    uow: Any, listing: Any, version: Any, rows: Sequence[Any]
) -> tuple[CanonicalListing, tuple[ContentFix, ...]] | None:
    """`content_fixes` on a stored version; None when the version or the adapter's limit rule is
    missing."""
    from app.adapters.registry import repairer
    from app.services.generation_context import previous_listing

    module = repairer(listing.marketplace)
    limit_rule = getattr(module, "content_limit", None)
    base = previous_listing(listing, version)
    candidates = [r for r in rows if fixable(r)]
    if limit_rule is None or base is None or not candidates:
        return None
    schema = await _stored_schema(uow, listing)

    def limit_of(row: Any) -> int | None:
        text = f"{row.raw_message or ''} {row.message or ''}"
        return limit_rule(schema, row.field_path, text)  # type: ignore[no-any-return]

    return content_fixes(base, candidates, limit_of)


async def _stored_schema(uow: Any, listing: Any) -> Any:
    from app.services.product_type_schema import current_schemas

    source = await uow.listings.preview_source(listing.id)
    pinned = None if source is None else source.template
    if pinned is None:
        return None
    schema, _parent = await current_schemas(
        uow, marketplace=listing.marketplace, product_type=pinned.product_type, with_parent=False
    )
    if schema is None:
        return None
    return await uow.ptd.schema_json(schema.definition_id)
