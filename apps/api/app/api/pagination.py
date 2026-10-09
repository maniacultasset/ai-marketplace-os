"""Keyset pagination (BACKEND-ARCHITECTURE.md §5.4; S3 D7).

Every list answers `{"data": [...], "page": {"next_cursor", "has_more", "limit"}}`. The cursor is
opaque to clients: unpadded base64url of `{"t": <aware ISO created_at>, "id": <uuid>}`, the keyset
`(created_at, id)`. There is no OFFSET or page-number parameter. `limit` defaults to 50, max 200.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import uuid
from datetime import datetime
from typing import Annotated, Final, NoReturn

from fastapi import Query
from mkos_schemas.base import WireModel
from mkos_schemas.codes import ErrorCode
from pydantic import Field

from app.api.errors import ApiError

DEFAULT_LIMIT: Final = 50
MAX_LIMIT: Final = 200
MAX_CURSOR_LENGTH: Final = 512
_CURSOR_ALPHABET: Final = re.compile(r"[A-Za-z0-9_-]+")
_CURSOR_KEYS: Final = frozenset({"t", "id"})
_TAGGED_KEYS: Final = frozenset({"o", "t", "id"})
_TAG: Final = re.compile(r"[a-z_]{1,16}")


class PageInfo(WireModel):
    next_cursor: Annotated[str | None, Field(max_length=MAX_CURSOR_LENGTH)] = None
    has_more: bool
    limit: Annotated[int, Field(ge=1, le=MAX_LIMIT)]


class Page[T](WireModel):
    """Subclass per resource (`class ProductPage(Page[ProductOut])`) so the component name is
    stable in openapi.json."""

    data: list[T]
    page: PageInfo


def encode_cursor(created_at: datetime, row_id: uuid.UUID) -> str:
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise ValueError("cursor timestamps must be timezone-aware")
    raw = json.dumps({"t": created_at.isoformat(), "id": str(row_id)}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("ascii")).decode("ascii").rstrip("=")


def _bad_cursor() -> NoReturn:
    raise ApiError(
        422,
        ErrorCode.VALIDATION_FAILED,
        details=[{"loc": ["query", "cursor"], "msg": "Invalid cursor."}],
    )


def _reject_constant(_: str) -> NoReturn:
    raise ValueError("non-finite number")


def encode_tagged_cursor(tag: str, at: datetime, row_id: uuid.UUID) -> str:
    """A cursor that also names the order it belongs to (S19: the listing list has two orders,
    so a cursor from one is refused by the other instead of skipping or repeating rows)."""
    if not _TAG.fullmatch(tag):
        raise ValueError("cursor tags are 1..16 lowercase letters or underscores")
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("cursor timestamps must be timezone-aware")
    raw = json.dumps({"o": tag, "t": at.isoformat(), "id": str(row_id)}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("ascii")).decode("ascii").rstrip("=")


def decode_tagged_cursor(cursor: str, tag: str) -> tuple[datetime, uuid.UUID]:
    """The keyset of a cursor made by `encode_tagged_cursor(tag, ...)`; 422 for any other."""
    value = _decode_json(cursor, _TAGGED_KEYS)
    if value.get("o") != tag:
        _bad_cursor()
    return _keyset(value)


def _decode_json(cursor: str, keys: frozenset[str]) -> dict[str, object]:
    if not 1 <= len(cursor) <= MAX_CURSOR_LENGTH or not _CURSOR_ALPHABET.fullmatch(cursor):
        _bad_cursor()
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        value = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (binascii.Error, UnicodeDecodeError, ValueError, OverflowError):
        _bad_cursor()
    if not isinstance(value, dict) or set(value) != keys:
        _bad_cursor()
    return value


def _keyset(value: dict[str, object]) -> tuple[datetime, uuid.UUID]:
    stamp, row = value["t"], value["id"]
    if not isinstance(stamp, str) or not isinstance(row, str):
        _bad_cursor()
    try:
        at = datetime.fromisoformat(stamp)
        row_id = uuid.UUID(row)
    except (ValueError, OverflowError):
        _bad_cursor()
    if at.tzinfo is None or at.utcoffset() is None:
        _bad_cursor()
    return at, row_id


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    """The keyset `(created_at, id)`, or `ApiError` 422 for anything that is not one of ours."""
    if not 1 <= len(cursor) <= MAX_CURSOR_LENGTH or not _CURSOR_ALPHABET.fullmatch(cursor):
        _bad_cursor()
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        value = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
        if not isinstance(value, dict) or set(value) != _CURSOR_KEYS:
            _bad_cursor()
        stamp, row = value["t"], value["id"]
        if not isinstance(stamp, str) or not isinstance(row, str):
            _bad_cursor()
        created_at = datetime.fromisoformat(stamp)
        row_id = uuid.UUID(row)
    except (binascii.Error, UnicodeDecodeError, ValueError, OverflowError):
        _bad_cursor()
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        _bad_cursor()
    return created_at, row_id


class PageParams:
    """`limit` and `cursor` for every list op. `after` is the decoded keyset, or None."""

    def __init__(
        self,
        limit: Annotated[
            int, Query(ge=1, le=MAX_LIMIT, description="Page size, 1..200 (default 50).")
        ] = DEFAULT_LIMIT,
        cursor: Annotated[
            str | None,
            Query(
                min_length=1,
                max_length=MAX_CURSOR_LENGTH,
                description="Opaque `page.next_cursor` from the previous page.",
            ),
        ] = None,
    ) -> None:
        self.limit = limit
        self.cursor = cursor
        self.after = decode_cursor(cursor) if cursor is not None else None


class TaggedPageParams:
    """`limit` and `cursor` for a list with more than one order (S19's listing list). The cursor
    stays encoded here: the service decodes it with the tag of the order it pages
    (`decode_tagged_cursor`), so a cursor from the other order is a 422."""

    def __init__(
        self,
        limit: Annotated[
            int, Query(ge=1, le=MAX_LIMIT, description="Page size, 1..200 (default 50).")
        ] = DEFAULT_LIMIT,
        cursor: Annotated[
            str | None,
            Query(
                min_length=1,
                max_length=MAX_CURSOR_LENGTH,
                description="Opaque `page.next_cursor` from the previous page.",
            ),
        ] = None,
    ) -> None:
        self.limit = limit
        self.cursor = cursor
