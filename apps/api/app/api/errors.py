"""The one HTTP error envelope (BACKEND-ARCHITECTURE.md §5.3; invariant 8).

Every non-2xx answer is `{"error": {type, message, status, correlation_id, details, error_report}}`.
`type` is an `ErrorCode`, `error_report` is the shared `ErrorReport` with one `request` item whose
`source` is `system` (CANON-ADDENDUM.md §19). Nothing from the request is echoed: messages are fixed
per status or code, 422 `details` carry only `{loc, msg}` with risky pydantic messages replaced, and
500 never carries exception text or a traceback.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Annotated, Any, Final

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import iter_route_contexts
from mkos_schemas.base import RequestId, WireModel
from mkos_schemas.codes import ERROR_CODE_META, ErrorCode
from mkos_schemas.enums import ErrorSource, RetryClass
from mkos_schemas.errors import ErrorItem, ErrorReport
from pydantic import Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.types import Scope

from app.core.errors import (
    CAP_RETRY_AFTER_S,
    ConflictError,
    InputError,
    LimitError,
    NotFoundError,
)
from app.core.flags import MarketplaceNotEnabled, not_enabled_message
from app.core.request_context import REQUEST_ID_PATTERN, current_request_id, new_request_id

CORRELATION_ID_PATTERN: Final = REQUEST_ID_PATTERN
"""Every `correlation_id` matches it: it is the request id (`app.core.request_context`)."""
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
        report: ErrorReport | None = None,
    ) -> None:
        self.status = status
        self.code = code
        self.report = report
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
    # The request middleware binds the request id (and owns the `X-Request-ID` header); a header
    # is never read here. Outside a request context (a bare app in a test) a fresh id stands in.
    chosen = current_request_id() or new_request_id()
    state[_STATE_KEY] = chosen
    return chosen


def correlation_id(request: Request) -> str:
    """The request's correlation id: the bound request id. Cached on the request so every
    envelope for one request carries the same id."""
    return _correlation_from_scope(request.scope)


def envelope_body(
    status: int,
    code: ErrorCode,
    message: str,
    correlation: str,
    details: Sequence[Mapping[str, Any]] | None = None,
    report: ErrorReport | None = None,
) -> dict[str, Any]:
    """The envelope. `report` replaces the default one-item `request` report when the failure
    has its own (an import's file-level `IMPORT_FAILED` report); it carries this request's id."""
    retryable = ERROR_CODE_META[code].retry_class is RetryClass.AUTO_RETRY
    if report is not None:
        report = report.model_copy(update={"request_id": correlation})
    else:
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


def envelope_headers(status: int, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    headers = dict(extra or {})
    if status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    if status == 429 and not any(name.lower() == "retry-after" for name in headers):
        # openapi.json declares Retry-After on every 429; a raiser that names no window gets
        # the per-tenant cap's fixed hint rather than none.
        headers["Retry-After"] = str(CAP_RETRY_AFTER_S)
    return headers


def error_response_for_scope(
    scope: Scope,
    status: int,
    code: ErrorCode | None = None,
    message: str | None = None,
    details: Sequence[Mapping[str, Any]] | None = None,
    headers: Mapping[str, str] | None = None,
    report: ErrorReport | None = None,
) -> JSONResponse:
    """The envelope as a response. Also used by pure ASGI middleware, which has only the scope."""
    correlation = _correlation_from_scope(scope)
    resolved = code or code_for_status(status)
    text = message or STATUS_MESSAGES.get(status) or STATUS_MESSAGES[500 if status >= 500 else 400]
    return JSONResponse(
        envelope_body(status, resolved, text, correlation, details, report),
        status_code=status,
        headers=envelope_headers(status, headers),
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
        request.scope, exc.status, exc.code, exc.message, exc.details, exc.headers, exc.report
    )


def _allowed_methods(request: Request) -> str | None:
    """Every method the resource at this path serves. Starlette's own `Allow` names only the
    first route whose path matched, so `DELETE /products` would advertise POST but not GET.
    Only the most specific matching routes count (the fewest path parameters): at
    `/listings/approve` that is the POST route, not `GET /listings/{listing_id}` (S26 fuzz)."""
    path = request.scope.get("path", "")
    by_params: dict[int, set[str]] = {}
    for candidate in iter_route_contexts(getattr(request.app, "routes", ())):
        regex = getattr(candidate, "path_regex", None)
        served = candidate.methods
        if regex is not None and served and regex.match(path):
            params = str(getattr(candidate, "path", "")).count("{")
            by_params.setdefault(params, set()).update(served)
    if not by_params:
        return None
    return ", ".join(sorted(by_params[min(by_params)]))


async def _http_exception(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)  # noqa: S101
    # `detail` is never echoed; only server-set headers (`Allow` on 405) are kept.
    headers = dict(exc.headers or {})
    if exc.status_code == 405:
        allowed = _allowed_methods(request)
        if allowed:
            headers["Allow"] = allowed
    return error_response_for_scope(request.scope, exc.status_code, headers=headers)


def _shadowed_static_allow(request: Request) -> str | None:
    """The methods of a static path (no parameters) that matches this request but does not
    serve its method, or None. `GET /listings/approve` otherwise reaches `GET
    /listings/{listing_id}` and fails its UUID check with a 422; the resource is the POST-only
    `/listings/approve`, so it is a 405 (S26 fix round 1, Schemathesis `unsupported_method`)."""
    path = request.scope.get("path", "")
    served: set[str] = set()
    for candidate in iter_route_contexts(getattr(request.app, "routes", ())):
        regex = getattr(candidate, "path_regex", None)
        methods = candidate.methods
        route_path = str(getattr(candidate, "path", ""))
        if regex is not None and methods and "{" not in route_path and regex.match(path):
            served.update(methods)
    if not served or request.method in served:
        return None
    return ", ".join(sorted(served))


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)  # noqa: S101
    allowed = _shadowed_static_allow(request)
    if allowed is not None:
        return error_response_for_scope(request.scope, 405, headers={"Allow": allowed})
    return error_response_for_scope(
        request.scope, 422, ErrorCode.VALIDATION_FAILED, details=validation_details(exc.errors())
    )


async def _not_implemented(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, NotImplementedYet)  # noqa: S101
    return error_response_for_scope(
        request.scope, 501, ErrorCode.NOT_IMPLEMENTED, f"{exc.op_id} is not implemented yet."
    )


# ---- domain errors (S12 T1): each becomes the `ApiError` a route would have raised ----

_INPUT_MESSAGES: Final[Mapping[ErrorCode, str]] = MappingProxyType(
    {ErrorCode.UNKNOWN_FACT_KEY: "Unknown key."}
)
_PATH_PART: Final = re.compile(r"([^.\[\]]+)|\[(\d{1,4})\]")


def path_segments(field: str) -> list[str | int]:
    """`images[0]` -> `["images", 0]`, `attributes.color` -> `["attributes", "color"]`. A part
    that is not a safe name becomes `<key>`, as in `validation_details`."""
    parts: list[str | int] = []
    for name, index in _PATH_PART.findall(field):
        parts.append(int(index) if index else _safe_loc_part(name))
    return parts[:_MAX_LOC_PARTS]


def domain_api_error(exc: Exception) -> ApiError:
    """The `ApiError` for a `DomainError` the routes let through (S12 T1)."""
    if isinstance(exc, ConflictError):
        if exc.code is ErrorCode.INTERNAL_ERROR:
            return ApiError(500, ErrorCode.INTERNAL_ERROR)
        return ApiError(409, exc.code)
    if isinstance(exc, NotFoundError):
        return ApiError(404, ErrorCode.NOT_FOUND, f"{path_label(exc.field)} not found.")
    if isinstance(exc, LimitError):
        return ApiError(429, exc.code, headers={"Retry-After": str(exc.retry_after_s)})
    if isinstance(exc, InputError):
        msg = _INPUT_MESSAGES.get(exc.code, "Invalid value.")
        return ApiError(
            422, exc.code, details=[{"loc": ["body", *path_segments(exc.field)], "msg": msg}]
        )
    raise TypeError(f"no HTTP mapping for {type(exc).__name__}")


def path_label(field: str) -> str:
    """The field name as the static message shows it: a server constant, or `resource`."""
    return field if _SAFE_LOC_PART.fullmatch(field) else "resource"


async def _domain_error(request: Request, exc: Exception) -> JSONResponse:
    return await _api_error(request, domain_api_error(exc))


async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    # Starlette's ServerErrorMiddleware calls this, sends the response, then re-raises `exc` to the
    # server, so the failure is still logged; the body is fixed text with no exception detail.
    return error_response_for_scope(request.scope, 500, ErrorCode.INTERNAL_ERROR)


def marketplace_not_enabled(marketplace: str) -> ApiError:
    """The one answer for a switched-off marketplace (S22 D2): 501 NOT_IMPLEMENTED."""
    return ApiError(501, ErrorCode.NOT_IMPLEMENTED, not_enabled_message(marketplace))


async def _not_enabled(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, MarketplaceNotEnabled)  # noqa: S101
    return await _api_error(request, marketplace_not_enabled(exc.marketplace.value))


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, _api_error)
    app.add_exception_handler(NotImplementedYet, _not_implemented)
    app.add_exception_handler(MarketplaceNotEnabled, _not_enabled)
    for domain in (ConflictError, NotFoundError, InputError, LimitError):
        app.add_exception_handler(domain, _domain_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_exception)
    app.add_exception_handler(Exception, _unhandled)
