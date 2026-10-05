"""The one HTTP error envelope (BACKEND-ARCHITECTURE.md §5.3; invariant 8).

Every non-2xx answer is `{"error": {type, message, status, correlation_id, details, error_report}}`.
`type` is an `ErrorCode`, `error_report` is the shared `ErrorReport` with one `request` item whose
`source` is `system` (CANON-ADDENDUM.md §19). Nothing from the request is echoed: messages are fixed
per status or code, 422 `details` carry only `{loc, msg}` with risky pydantic messages replaced, and
500 never carries exception text or a traceback.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Annotated, Any, Final

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from mkos_schemas.base import RequestId, WireModel
from mkos_schemas.codes import ERROR_CODE_META, ErrorCode
from mkos_schemas.enums import ErrorSource, RetryClass
from mkos_schemas.errors import ErrorItem, ErrorReport
from pydantic import Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.types import Scope

CORRELATION_ID_PATTERN: Final = r"[A-Za-z0-9._:-]{1,128}"
"""Inbound `X-Request-ID` kept as-is only when it matches (S11 D3, docs/plans/s11/PLAN.md:128)."""
_CORRELATION_RE: Final = re.compile(CORRELATION_ID_PATTERN)
REQUEST_ID_HEADER: Final = "X-Request-ID"
_STATE_KEY: Final = "correlation_id"

STATUS_CODES: Final[Mapping[int, ErrorCode]] = MappingProxyType(
    {
        400: ErrorCode.VALIDATION_FAILED,
        401: ErrorCode.UNAUTHENTICATED,
        403: ErrorCode.PERMISSION_DENIED,
        404: ErrorCode.NOT_FOUND,
        405: ErrorCode.METHOD_NOT_ALLOWED,
        409: ErrorCode.IDEMPOTENCY_KEY_REUSED,
        413: ErrorCode.PAYLOAD_TOO_LARGE,
        422: ErrorCode.VALIDATION_FAILED,
        429: ErrorCode.RATE_LIMITED,
        501: ErrorCode.NOT_IMPLEMENTED,
    }
)
"""The default code per HTTP status (S3 D2). A handler may name a more specific one (`ApiError`)."""

STATUS_MESSAGES: Final[Mapping[int, str]] = MappingProxyType(
    {
        400: "The request could not be parsed.",
        401: "Authentication required.",
        403: "Permission denied.",
        404: "Not found.",
        405: "Method not allowed.",
        409: "The request conflicts with the current state.",
        413: "Request body too large.",
        422: "Request validation failed.",
        429: "Too many requests.",
        500: "Internal server error.",
        501: "Not implemented yet.",
    }
)

MAX_DETAILS: Final = 50
_MAX_LOC_PARTS: Final = 16
_SAFE_LOC_PART: Final = re.compile(r"[A-Za-z0-9_.\-\[\]]{1,64}")
_MAX_MSG: Final = 200

# pydantic messages that quote the input (uuid_parsing names the bad character; value_error and
# assertion_error carry a validator's own text) are replaced. `mkos_invalid` is the type our own
# validators raise through PydanticCustomError, whose text is a server constant.
_REPLACED_MESSAGES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "value_error": "Invalid value.",
        "assertion_error": "Invalid value.",
        "uuid_parsing": "Input should be a valid UUID.",
        "uuid_type": "Input should be a valid UUID.",
        "uuid_version": "Input should be a valid UUID.",
        "json_invalid": "Malformed JSON body.",
        "url_parsing": "Input should be a valid URL.",
        "url_syntax_violation": "Input should be a valid URL.",
    }
)


class ErrorDetail(WireModel):
    loc: list[str | int]
    msg: str


class ErrorBody(WireModel):
    type: ErrorCode
    message: Annotated[str, Field(min_length=1, max_length=1000)]
    status: Annotated[int, Field(ge=400, le=599)]
    correlation_id: RequestId
    details: list[ErrorDetail] | None = None
    error_report: ErrorReport


class ErrorEnvelope(WireModel):
    error: ErrorBody


class ApiError(Exception):
    """Raised by routes and dependencies; rendered as the envelope with this status and code."""

    def __init__(
        self,
        status: int,
        code: ErrorCode,
        message: str | None = None,
        details: Sequence[Mapping[str, Any]] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.status = status
        self.code = code
        self.message = message or STATUS_MESSAGES.get(status, STATUS_MESSAGES[500])
        self.details = [dict(d) for d in details] if details is not None else None
        self.headers = dict(headers or {})
        super().__init__(f"{status} {code.value}")


class NotImplementedYet(Exception):  # noqa: N818 - CODE-PLAN s3 T3 name
    """A frozen `/api/v1` operation whose owner step has not landed yet: 501 NOT_IMPLEMENTED."""

    def __init__(self, op_id: str) -> None:
        self.op_id = op_id
        super().__init__(op_id)


def code_for_status(status: int) -> ErrorCode:
    if status in STATUS_CODES:
        return STATUS_CODES[status]
    if status >= 500:
        return ErrorCode.INTERNAL_ERROR
    return ErrorCode.VALIDATION_FAILED


def _correlation_from_scope(scope: Scope) -> str:
    state = scope.setdefault("state", {})
    cached = state.get(_STATE_KEY)
    if isinstance(cached, str):
        return cached
    values = [v for k, v in scope.get("headers", ()) if k.lower() == b"x-request-id"]
    value = values[0].decode("latin-1") if len(values) == 1 else ""
    chosen = value if _CORRELATION_RE.fullmatch(value) else str(uuid.uuid4())
    state[_STATE_KEY] = chosen
    return chosen


def correlation_id(request: Request) -> str:
    """The request's correlation id: a valid inbound `X-Request-ID`, else a new UUID. Cached on the
    request so every envelope for one request carries the same id (S11 replaces this helper)."""
    return _correlation_from_scope(request.scope)


def envelope_body(
    status: int,
    code: ErrorCode,
    message: str,
    correlation: str,
    details: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    retryable = ERROR_CODE_META[code].retry_class is RetryClass.AUTO_RETRY
    item = ErrorItem(
        field="request",
        error_code=code,
        message=message,
        source=ErrorSource.SYSTEM,
        retryable=retryable,
    )
    report = ErrorReport(
        status="failed",
        error_code=code,
        retryable=retryable,
        errors=[item],
        request_id=correlation,
    )
    body = ErrorEnvelope(
        error=ErrorBody(
            type=code,
            message=message,
            status=status,
            correlation_id=correlation,
            details=(
                [ErrorDetail.model_validate(d) for d in details] if details is not None else None
            ),
            error_report=report,
        )
    )
    return body.model_dump(mode="json", by_alias=True)


def envelope_headers(
    status: int, correlation: str, extra: Mapping[str, str] | None = None
) -> dict[str, str]:
    headers = dict(extra or {})
    headers[REQUEST_ID_HEADER] = correlation
    if status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return headers


def error_response_for_scope(
    scope: Scope,
    status: int,
    code: ErrorCode | None = None,
    message: str | None = None,
    details: Sequence[Mapping[str, Any]] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """The envelope as a response. Also used by pure ASGI middleware, which has only the scope."""
    correlation = _correlation_from_scope(scope)
    resolved = code or code_for_status(status)
    text = message or STATUS_MESSAGES.get(status) or STATUS_MESSAGES[500 if status >= 500 else 400]
    return JSONResponse(
        envelope_body(status, resolved, text, correlation, details),
        status_code=status,
        headers=envelope_headers(status, correlation, headers),
    )


def _safe_loc_part(part: object) -> str | int:
    if isinstance(part, int) and not isinstance(part, bool):
        return part
    text = str(part)
    return text if _SAFE_LOC_PART.fullmatch(text) else "<key>"


def _safe_msg(error: Mapping[str, Any]) -> str:
    kind = str(error.get("type", ""))
    if kind in _REPLACED_MESSAGES:
        return _REPLACED_MESSAGES[kind]
    msg = str(error.get("msg") or "Invalid value.")[:_MAX_MSG]
    raw = error.get("input")
    if isinstance(raw, str | int | float) and not isinstance(raw, bool):
        text = str(raw)
        if len(text) >= 3 and text in msg:
            return "Invalid value."
    return msg


def validation_details(errors: Sequence[Any]) -> list[dict[str, Any]]:
    """`{loc, msg}` only: never `input`, `ctx` or `url` (AC3). Unsafe key names become `<key>`."""
    out: list[dict[str, Any]] = []
    for error in list(errors)[:MAX_DETAILS]:
        if not isinstance(error, Mapping):
            continue
        loc = [_safe_loc_part(p) for p in tuple(error.get("loc", ()))[:_MAX_LOC_PARTS]]
        out.append({"loc": loc, "msg": _safe_msg(error)})
    return out


async def _api_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiError)  # noqa: S101 - handler registered for ApiError only
    return error_response_for_scope(
        request.scope, exc.status, exc.code, exc.message, exc.details, exc.headers
    )


async def _http_exception(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)  # noqa: S101
    # `detail` is never echoed; only server-set headers (`Allow` on 405) are kept.
    return error_response_for_scope(request.scope, exc.status_code, headers=dict(exc.headers or {}))


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)  # noqa: S101
    return error_response_for_scope(
        request.scope, 422, ErrorCode.VALIDATION_FAILED, details=validation_details(exc.errors())
    )


async def _not_implemented(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, NotImplementedYet)  # noqa: S101
    return error_response_for_scope(
        request.scope, 501, ErrorCode.NOT_IMPLEMENTED, f"{exc.op_id} is not implemented yet."
    )


async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    # Starlette's ServerErrorMiddleware calls this, sends the response, then re-raises `exc` to the
    # server, so the failure is still logged; the body is fixed text with no exception detail.
    return error_response_for_scope(request.scope, 500, ErrorCode.INTERNAL_ERROR)


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, _api_error)
    app.add_exception_handler(NotImplementedYet, _not_implemented)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_exception)
    app.add_exception_handler(Exception, _unhandled)
