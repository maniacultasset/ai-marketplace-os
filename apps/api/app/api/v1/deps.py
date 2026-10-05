"""Shared `/api/v1` request dependencies (S3 T5; D9).

`bearer_required` checks only that a Bearer credential is present: S3's stubs verify nothing, so a
missing or non-Bearer `Authorization` header is a real 401 now. S10 replaces it with the verified
principal (`get_current_principal`), and the case-matrix guard fails any implemented route that
still depends on it.
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import Header, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from mkos_schemas.codes import ErrorCode

from app.api.errors import ApiError

IDEMPOTENCY_KEY_PATTERN: Final = r"^[\x21-\x7E]{1,255}$"
IF_MATCH_PATTERN: Final = r'^"[0-9a-f-]{36}"$'

_bearer = HTTPBearer(auto_error=False, description="Access token (JWT) from `/api/v1/auth/login`.")


async def bearer_required(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(_bearer)],
) -> None:
    """Presence only: no header, an empty credential or another scheme is 401 UNAUTHENTICATED."""
    if credentials is None or not credentials.credentials.strip():
        raise ApiError(401, ErrorCode.UNAUTHENTICATED)


IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        pattern=IDEMPOTENCY_KEY_PATTERN,
        description=(
            "1-255 visible ASCII characters. The same key with the same body replays the stored "
            "response; with a different body it is 409 IDEMPOTENCY_KEY_REUSED (24 h)."
        ),
    ),
]

IfMatch = Annotated[
    str,
    Header(
        alias="If-Match",
        pattern=IF_MATCH_PATTERN,
        description=(
            'The quoted `current_version_id` the edit is based on, e.g. "<uuid>". '
            "A stale value is 409 STALE_VERSION."
        ),
    ),
]
