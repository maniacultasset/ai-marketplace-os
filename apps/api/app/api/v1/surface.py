"""The frozen `/api/v1` surface: one row per operation (S3; BACKEND-ARCHITECTURE.md §5.2).

`OPS` is the frozen list (35 operations at S3, 38 with S10's switch and members, 40 with S15's
connect confirm and account revoke, 43 with S16's profile update and document list and delete,
44 with S20's listing preview, 48 with S19's listing history, issues, issue dismiss and tasks,
50 with S21's listing submissions and refresh-status; no
dead-letter path,
which is staff tooling per T82). It
drives the routers (`route()`), the declared OpenAPI responses (`declared_responses()`), the
parametrized surface tests and the case-matrix guard (`cases()`), so a route, its contract and its
required tests cannot drift apart.

Each operation names the one action it requires (`action`, CODE-PLAN S10 Q7; None for a public
op), and `roles` is derived from it through the role x action matrix (`app/core/rbac.py`), so the
registry, the runtime `require()` check and the declared 403s cannot drift apart. `cases()` and
`declared_responses()` read only `op.roles`. Every operation carries `x-mkos-action` in OpenAPI.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final, Literal, get_args

from fastapi import APIRouter
from fastapi.responses import Response
from mkos_schemas.auth import Role
from mkos_schemas.codes import ErrorCode
from mkos_schemas.imports import ImportReport
from mkos_schemas.jobs import JobAccepted, JobEnvelope
from mkos_schemas.publish import MarketplacePreview
from mkos_schemas.validation import ValidationResult, ValidationSummary
from pydantic import BaseModel

from app.api.errors import STATUS_MESSAGES, ErrorEnvelope, envelope_body
from app.api.v1.deps import public, require
from app.api.v1.examples import CALLBACK_LOCATION, ETAG_EXAMPLE, SUCCESS_EXAMPLES
from app.api.v1.schemas.analytics import AnalyticsSummary
from app.api.v1.schemas.auth import MemberOut, MemberPage, MeOut, TokenResponse
from app.api.v1.schemas.batches import BatchAccepted, BatchResult
from app.api.v1.schemas.brand import BrandDocumentPage
from app.api.v1.schemas.images import ImageAccepted, ImageReportOut, UploadUrlOut
from app.api.v1.schemas.imports import ImportAccepted, ImportRowPage
from app.api.v1.schemas.issues import IssueOut, IssuePage
from app.api.v1.schemas.listings import (
    ListingDetail,
    ListingHistoryPage,
    ListingPage,
    ListingStatusOut,
)
from app.api.v1.schemas.marketplaces import (
    ConnectOut,
    MarketplaceAccountOut,
    MarketplaceAccountPage,
)
from app.api.v1.schemas.products import ProductOut, ProductPage
from app.api.v1.schemas.rules import RulePage
from app.api.v1.schemas.submissions import SubmissionPage
from app.api.v1.schemas.tasks import TaskPage
from app.api.v1.schemas.tenants import TenantOut
from app.core.rbac import PUBLIC, Action, roles_for

Case = Literal[
    "success",
    "validation",
    "missing_auth",
    "invalid_token",
    "wrong_role",
    "wrong_tenant",
    "not_found",
    "conflict",
    "maker_checker",
    "bad_state",
]
CASES: Final[tuple[Case, ...]] = get_args(Case)
ALL_ROLES: Final[frozenset[Role]] = frozenset(get_args(Role))
NO_ROLES: Final[frozenset[Role]] = frozenset()
Method = Literal["GET", "POST", "PATCH", "DELETE"]
EXAMPLE_CORRELATION_ID: Final = "req-example-0001"

E = ErrorCode


@dataclass(frozen=True, slots=True)
class Operation:
    op_id: str
    method: Method
    path: str
    """Without the `/api/v1` prefix (it comes from `settings.api_v1_prefix`)."""
    status: int
    response_model: type[BaseModel] | None
    group: str
    summary: str
    owner_step: str
    public: bool = False
    path_id: str | None = None
    """The path parameter naming a tenant-owned row: forces not_found and wrong_tenant (404)."""
    body_ids: tuple[str, ...] = ()
    """Body fields naming tenant-owned rows or objects (a `StorageKey` counts, CP6)."""
    query: tuple[str, ...] = ()
    has_body: bool = False
    idempotency: bool = False
    if_match: bool = False
    conflict_code: ErrorCode | None = None
    """The op's own 409 code (SKU_CONFLICT, STALE_VERSION, INVALID_STATUS_TRANSITION)."""
    maker_checker: bool = False
    oauth_state: bool = False
    etag: bool = False
    """The success response carries an `ETag` header (S16 CP3: the version a PATCH sends back)."""
    action: Action | None = None
    """The action `require()` checks (CODE-PLAN S10 Q7); None exactly when the op is public."""
    roles: frozenset[Role] | None = None
    """Derived from `action` through the matrix when not given; a public op holds ALL_ROLES."""
    extra_codes: Mapping[int, tuple[ErrorCode, ...]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    """Codes beyond the D2 default for a status (login's 401, products_create's UNKNOWN_FACT_KEY),
    or a status of the op's own (listings_preview's 501 for a marketplace with no adapter)."""

    def __post_init__(self) -> None:
        if (self.action is None) is not self.public:
            raise ValueError(f"{self.op_id}: a public op has no action; any other op needs one")
        if self.roles is None:
            derived = ALL_ROLES if self.action is None else roles_for(self.action)
            object.__setattr__(self, "roles", derived)

    @property
    def policy(self) -> str:
        """The `x-mkos-action` value: the action, or `public`."""
        return PUBLIC if self.action is None else self.action.value

    @property
    def has_input(self) -> bool:
        return bool(
            "{" in self.path or self.has_body or self.query or self.idempotency or self.if_match
        )

    @property
    def error_codes(self) -> Mapping[int, tuple[ErrorCode, ...]]:
        """Every declared non-2xx status and its codes (the first one is the example's `type`)."""
        return _error_codes(self)


def cases(op: Operation) -> frozenset[Case]:
    """The test cases an implemented route must carry (AC9). Reads only the op's own fields."""
    out: set[Case] = {"success"}
    if op.has_input:
        out.add("validation")
    if not op.public:
        out |= {"missing_auth", "invalid_token", "wrong_tenant"}
        if op.roles != ALL_ROLES:
            out.add("wrong_role")
    if op.path_id or op.body_ids:
        out.add("not_found")
    if op.conflict_code is not None or op.idempotency:
        out.add("conflict")
    if op.maker_checker:
        out.add("maker_checker")
    if op.oauth_state:
        out.add("bad_state")
    return frozenset(out)


def _error_codes(op: Operation) -> Mapping[int, tuple[ErrorCode, ...]]:
    c = cases(op)
    extra = op.extra_codes
    out: dict[int, tuple[ErrorCode, ...]] = {}
    if "validation" in c:
        out[422] = (E.VALIDATION_FAILED, *extra.get(422, ()))
    if "missing_auth" in c or 401 in extra:
        out[401] = (E.UNAUTHENTICATED,)
    forbidden = [E.PERMISSION_DENIED] if "wrong_role" in c else []
    if "maker_checker" in c:
        forbidden.append(E.MAKER_CHECKER_VIOLATION)
    if forbidden:
        out[403] = tuple(forbidden)
    if "not_found" in c:
        out[404] = (E.NOT_FOUND,)
    conflicts = [E.IDEMPOTENCY_KEY_REUSED] if op.idempotency else []
    if op.conflict_code is not None:
        conflicts.append(op.conflict_code)
    if "bad_state" in c:
        conflicts.append(E.OAUTH_STATE_INVALID)
    if conflicts:
        out[409] = tuple(conflicts)
    if op.has_body:
        out[413] = (E.PAYLOAD_TOO_LARGE,)
    out[429] = (E.RATE_LIMITED,)
    for status, codes in extra.items():
        # an op's own codes join the default ones for a status the cases already declare
        out[status] = (*out.get(status, ()), *(c for c in codes if c not in out.get(status, ())))
    return MappingProxyType(dict(sorted(out.items())))


def case_status(op: Operation, case: Case) -> int:
    """The HTTP status a case answers with (wrong_tenant on a list is a filtered 2xx)."""
    statuses: dict[Case, int] = {
        "validation": 422,
        "missing_auth": 401,
        "invalid_token": 401,
        "wrong_role": 403,
        "maker_checker": 403,
        "not_found": 404,
        "conflict": 409,
        "bad_state": 409,
    }
    if case == "wrong_tenant":
        return 404 if "not_found" in cases(op) else op.status
    return statuses.get(case, op.status)


_EXAMPLE_DETAILS: Final = ({"loc": ["body", "field"], "msg": "Field required"},)


def error_example(status: int, code: ErrorCode) -> dict[str, Any]:
    details = list(_EXAMPLE_DETAILS) if status == 422 else None
    message = STATUS_MESSAGES.get(status, STATUS_MESSAGES[500])
    return envelope_body(status, code, message, EXAMPLE_CORRELATION_ID, details)


def _codes_text(codes: tuple[ErrorCode, ...]) -> str:
    return ", ".join(f"`{c.value}`" for c in codes)


RETRY_AFTER_HEADER: Final[Mapping[str, Any]] = MappingProxyType(
    {
        "description": (
            "Whole seconds to wait before retrying. Every 429 carries it: the limiter's window, "
            "the rest of a per-listing window, or a fixed hint for a per-tenant cap."
        ),
        "schema": {"type": "integer", "minimum": 1},
        "example": 30,
    }
)


def declared_responses(op: Operation) -> dict[int | str, dict[str, Any]]:
    """The non-2xx responses the op declares, each `ErrorEnvelope` with an example (AC5, AC12);
    every 429 also declares its `Retry-After` header."""
    out: dict[int | str, dict[str, Any]] = {}
    for status, codes in op.error_codes.items():
        out[status] = {
            "model": ErrorEnvelope,
            "description": f"{STATUS_MESSAGES.get(status, 'Error')} Codes: {_codes_text(codes)}.",
            "content": {"application/json": {"example": error_example(status, codes[0])}},
        }
        if status == 429:
            out[status]["headers"] = {"Retry-After": dict(RETRY_AFTER_HEADER)}
    out["default"] = {
        "model": ErrorEnvelope,
        "description": (
            "Any other failure, in the same envelope: 501 `NOT_IMPLEMENTED` until the owner "
            "step lands, 405 `METHOD_NOT_ALLOWED`, 500 `INTERNAL_ERROR`."
        ),
        "content": {
            "application/json": {"example": error_example(500, E.INTERNAL_ERROR)},
        },
    }
    return out


def success_response(op: Operation) -> dict[int | str, dict[str, Any]]:
    """The 2xx/3xx entry: the op's success example, or the 303 `Location` / 204 description."""
    if op.status == 303:
        return {
            303: {
                "description": (
                    "Redirect to the app. `Location` carries only an opaque single-use "
                    "`pending` handle: no code, token or account data."
                ),
                "headers": {
                    "Location": {
                        "description": "The fixed app route plus `?pending=<opaque>`.",
                        "schema": {"type": "string"},
                        "example": CALLBACK_LOCATION,
                    }
                },
            }
        }
    if op.response_model is None:
        return {op.status: {"description": "Done; no body."}}
    entry: dict[str, Any] = {
        "content": {"application/json": {"example": SUCCESS_EXAMPLES[op.op_id]}}
    }
    if op.etag:
        entry["headers"] = {
            "ETag": {
                "description": (
                    "The current version (of the profile or the listing), a quoted UUID. Send "
                    "it back as `If-Match` on the next PATCH."
                ),
                "schema": {"type": "string"},
                "example": ETAG_EXAMPLE,
            }
        }
    return {op.status: entry}


def stub[F: Callable[..., Any]](fn: F) -> F:
    """Marks a handler as the S3 501 stub; the case-matrix guard skips it."""
    fn.__mkos_stub__ = True  # type: ignore[attr-defined]
    return fn


def is_stub(fn: object) -> bool:
    return getattr(fn, "__mkos_stub__", False) is True


def _policy(op: Operation) -> Any:
    if op.action is None:
        return public()
    return require(op.action)


def route(
    router: APIRouter, op: Operation, **extra: Any
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register `fn` for `op`: method, path, status, response model, declared responses,
    `operation_id`, tag, `x-mkos-action`, and the op's policy: `require(op.action)` (the verified
    principal, the tenant limiter and the role check) or, for a public op, `public()` (the per-IP
    limiter). A route therefore cannot leave out its role check or its limiter."""

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        kwargs: dict[str, Any] = dict(extra)
        if op.response_model is None:
            kwargs.setdefault("response_class", Response)
        router.add_api_route(
            op.path,
            fn,
            methods=[op.method],
            status_code=op.status,
            response_model=op.response_model,
            responses=success_response(op) | declared_responses(op),
            operation_id=op.op_id,
            summary=op.summary,
            tags=[op.group],
            dependencies=[_policy(op)],
            openapi_extra={"x-mkos-action": op.policy},
            **kwargs,
        )
        return fn

    return decorate


A = Action
_401: Final = MappingProxyType({401: (E.UNAUTHENTICATED,)})
# S22 D2: a marketplace that is not configured or is switched off (FLIPKART_ENABLED=false).
_501: Final = MappingProxyType({501: (E.NOT_IMPLEMENTED,)})

_ROWS: Final[tuple[Operation, ...]] = (
    # ---- auth (S10) ----
    Operation("auth_login", "POST", "/auth/login", 200, TokenResponse, "auth",
              "Log in with email and password", "S10", public=True, has_body=True, extra_codes=_401),
    Operation("auth_refresh", "POST", "/auth/refresh", 200, TokenResponse, "auth",
              "Rotate the refresh token", "S10", public=True, has_body=True,
              extra_codes=_401),
    Operation("auth_logout", "POST", "/auth/logout", 204, None, "auth",
              "Revoke the refresh token", "S10", public=True, has_body=True),
    Operation("auth_me", "GET", "/auth/me", 200, MeOut, "auth",
              "The current principal and memberships", "S10", action=A.TENANT_READ),
    Operation("auth_switch_tenant", "POST", "/auth/switch-tenant", 200, TokenResponse, "auth",
              "Switch the active tenant: a new token pair for another of the caller's tenants",
              "S10", has_body=True, body_ids=("target_tenant_id",), action=A.TENANT_READ),
    # ---- members (S10) ----
    Operation("members_list", "GET", "/members", 200, MemberPage, "members",
              "List the tenant's members", "S10", query=("limit", "cursor"),
              action=A.TEAM_MANAGE),
    Operation("members_patch", "PATCH", "/members/{user_id}", 200, MemberOut, "members",
              "Change a member's role or active flag (only an owner grants or removes owner)",
              "S10", path_id="user_id", has_body=True, conflict_code=E.LAST_OWNER_REQUIRED,
              action=A.TEAM_MANAGE),
    # ---- tenants and brand (S16) ----
    Operation("tenants_me", "GET", "/tenants/me", 200, TenantOut, "tenants",
              "The brand profile", "S16", etag=True, action=A.TENANT_READ),
    Operation("tenants_me_update", "PATCH", "/tenants/me", 200, TenantOut, "tenants",
              "Update the brand profile (If-Match: the ETag from GET)", "S16", has_body=True,
              if_match=True, conflict_code=E.STALE_VERSION, etag=True,
              action=A.SETTINGS_MANAGE),
    Operation("brand_documents_upload", "POST", "/brand/documents", 202, JobAccepted, "brand",
              "Upload a brand document for chunking", "S16", has_body=True, idempotency=True, action=A.SETTINGS_MANAGE),
    Operation("brand_documents_list", "GET", "/brand/documents", 200, BrandDocumentPage,
              "brand", "List the brand documents", "S16", query=("limit", "cursor"),
              action=A.TENANT_READ),
    Operation("brand_documents_delete", "DELETE", "/brand/documents/{document_id}", 204, None,
              "brand", "Delete a brand document and deactivate its chunks", "S16",
              path_id="document_id", action=A.SETTINGS_MANAGE),
    # ---- products (S12, S13) ----
    Operation("products_create", "POST", "/products", 201, ProductOut, "products",
              "Create one product", "S12", has_body=True, body_ids=("images",),
              conflict_code=E.SKU_CONFLICT,
              extra_codes=MappingProxyType({422: (E.UNKNOWN_FACT_KEY,)}), action=A.PRODUCT_IMPORT),
    Operation("products_list", "GET", "/products", 200, ProductPage, "products",
              "List products", "S12", query=("limit", "cursor"), action=A.TENANT_READ),
    Operation("products_get", "GET", "/products/{product_id}", 200, ProductOut, "products",
              "Read one product", "S12", path_id="product_id", action=A.TENANT_READ),
    Operation("products_bulk", "POST", "/products/bulk", 202, ImportAccepted, "products",
              "Import products from a CSV file", "S13", has_body=True, idempotency=True,
              extra_codes=MappingProxyType({422: (E.IMPORT_FAILED,)}), action=A.PRODUCT_IMPORT),
    # ---- imports (S13) ----
    Operation("imports_get", "GET", "/imports/{import_id}", 200, ImportReport, "imports",
              "The import report (counts; rows via /rows)", "S13", path_id="import_id", action=A.TENANT_READ),
    Operation("imports_rows", "GET", "/imports/{import_id}/rows", 200, ImportRowPage, "imports",
              "Import rows, optionally only failed ones", "S13", path_id="import_id",
              query=("status", "limit", "cursor"), action=A.TENANT_READ),
    Operation("imports_rows_retry", "POST", "/imports/{import_id}/rows/retry", 202, JobAccepted,
              "imports", "Re-run the failed rows only", "S13", path_id="import_id",
              idempotency=True, conflict_code=E.INVALID_STATUS_TRANSITION, action=A.PRODUCT_IMPORT),
    # ---- listings (S18, S19, S21) ----
    Operation("listings_generate", "POST", "/listings/generate", 202, JobAccepted, "listings",
              "Start AI generation", "S18", has_body=True, body_ids=("product_id",),
              idempotency=True, action=A.LISTING_GENERATE),
    Operation("listings_validate", "POST", "/listings/validate", 200, ValidationSummary,
              "listings", "Run schema, rules and vision checks (200 with passed=false on a rules "
              "failure)", "S18", has_body=True, body_ids=("listing_id",), action=A.LISTING_EDIT),
    Operation("listings_approve", "POST", "/listings/approve", 200, BatchResult, "listings",
              "Approve or reject a batch (maker-checker enforced)", "S19", has_body=True,
              body_ids=("items[].listing_id",), idempotency=True, maker_checker=True, action=A.LISTING_APPROVE),
    Operation("listings_publish", "POST", "/listings/publish", 202, BatchAccepted, "listings",
              "Publish approved listings", "S21", has_body=True,
              body_ids=("items[].listing_id",), idempotency=True, action=A.LISTING_PUBLISH),
    Operation("listings_list", "GET", "/listings", 200, ListingPage, "listings",
              "List listings (the review queue)", "S19",
              query=("status", "marketplace", "product_id", "limit", "cursor"), action=A.TENANT_READ),
    Operation("listings_get", "GET", "/listings/{listing_id}", 200, ListingDetail, "listings",
              "Read one listing with its content and validation", "S19", path_id="listing_id",
              etag=True, action=A.TENANT_READ),
    Operation("listings_preview", "GET", "/listings/{listing_id}/preview", 200,
              MarketplacePreview, "listings", "Preview the marketplace payload of the current "
              "version (sends nothing)", "S20", path_id="listing_id", conflict_code=E.LISTING_BUSY,
              extra_codes=MappingProxyType({501: (E.NOT_IMPLEMENTED,)}), action=A.TENANT_READ),
    Operation("listings_status", "GET", "/listings/{listing_id}/status", 200, ListingStatusOut,
              "listings", "Listing status (a convenience alias; poll jobs instead)", "S18",
              path_id="listing_id", action=A.TENANT_READ),
    Operation("listings_history", "GET", "/listings/{listing_id}/history", 200,
              ListingHistoryPage, "listings", "The listing's audit history, newest first", "S19",
              path_id="listing_id", query=("limit", "cursor"), action=A.TENANT_READ),
    Operation("listings_content_patch", "PATCH", "/listings/{listing_id}/content", 200,
              ListingDetail, "listings", "Edit content (creates a new version)", "S18",
              path_id="listing_id", has_body=True, if_match=True,
              conflict_code=E.STALE_VERSION, etag=True, action=A.LISTING_EDIT),
    Operation("listings_regenerate", "POST", "/listings/{listing_id}/regenerate", 202,
              JobAccepted, "listings", "Regenerate an existing listing", "S18",
              path_id="listing_id", has_body=True, idempotency=True, action=A.LISTING_REGENERATE),
    Operation("listings_retry_fields", "POST", "/listings/{listing_id}/publish/retry-fields",
              202, JobAccepted, "listings", "Retry only the fields that failed to publish",
              "S21", path_id="listing_id", idempotency=True,
              conflict_code=E.DUPLICATE_SUBMISSION_BLOCKED,
              extra_codes=MappingProxyType({409: (
                  E.INVALID_STATUS_TRANSITION, E.LISTING_BUSY, E.SUBMISSION_ATTEMPTS_EXHAUSTED,
                  E.AUTH_EXPIRED), **_501}),
              action=A.LISTING_PUBLISH),
    Operation("listings_submissions", "GET", "/listings/{listing_id}/submissions", 200,
              SubmissionPage, "listings", "The listing's publish attempts with their per-field "
              "results, newest first", "S21", path_id="listing_id", query=("limit", "cursor"),
              action=A.TENANT_READ),
    Operation("listings_refresh_status", "POST", "/listings/{listing_id}/refresh-status", 202,
              JobAccepted, "listings", "Check the marketplace status now (at most once per 5 "
              "minutes per listing)", "S21", path_id="listing_id", idempotency=True,
              conflict_code=E.INVALID_STATUS_TRANSITION,
              extra_codes=MappingProxyType({409: (E.LISTING_BUSY,)}), action=A.LISTING_PUBLISH),
    # ---- marketplaces (S15) ----
    Operation("marketplaces_connect", "POST", "/marketplaces/connect", 200, ConnectOut,
              "marketplaces", "Start OAuth; returns the consent URL", "S15", has_body=True,
              extra_codes=_501, action=A.MARKETPLACE_CONNECT),
    Operation("marketplaces_callback", "GET", "/marketplaces/{marketplace}/callback", 303, None,
              "marketplaces", "OAuth return: 303 to the app with an opaque pending handle",
              "S15", public=True,
              query=("code", "state", "spapi_oauth_code", "selling_partner_id"), oauth_state=True,
              extra_codes=_501),
    Operation("marketplaces_accounts", "GET", "/marketplaces/accounts", 200,
              MarketplaceAccountPage, "marketplaces", "Connected accounts and their health",
              "S15", query=("limit", "cursor"), action=A.TENANT_READ),
    Operation("marketplaces_connect_confirm", "POST", "/marketplaces/connect/confirm", 200,
              MarketplaceAccountOut, "marketplaces",
              "Confirm a pending connection: the account joins the caller's tenant", "S15",
              has_body=True, conflict_code=E.MARKETPLACE_ACCOUNT_CONFLICT, oauth_state=True,
              extra_codes=_501, action=A.MARKETPLACE_CONNECT),
    Operation("marketplaces_accounts_revoke", "POST", "/marketplaces/accounts/{account_id}/revoke",
              200, MarketplaceAccountOut, "marketplaces",
              "Revoke a connection: delete the stored tokens", "S15", path_id="account_id",
              action=A.MARKETPLACE_CONNECT),
    # ---- images (S17b) ----
    Operation("images_upload_url", "POST", "/images/upload-url", 200, UploadUrlOut, "images",
              "A signed upload URL for one image", "S17b", has_body=True,
              body_ids=("product_id",), action=A.PRODUCT_IMPORT),
    Operation("images_upload", "POST", "/images/upload", 202, ImageAccepted, "images",
              "Upload an image (or name an uploaded key) and start the checks", "S17b",
              has_body=True, body_ids=("product_id", "storage_key"), idempotency=True, action=A.PRODUCT_IMPORT),
    Operation("images_report", "GET", "/images/{image_id}/report", 200, ImageReportOut, "images",
              "The image compliance reports", "S17b", path_id="image_id", action=A.TENANT_READ),
    # ---- rules (S14b) ----
    Operation("rules_list", "GET", "/rules", 200, RulePage, "rules", "List rules", "S14b",
              query=("marketplace", "category", "limit", "cursor"), action=A.TENANT_READ),
    Operation("rules_dry_run", "POST", "/rules/dry-run", 200, ValidationResult, "rules",
              "Run the Rules Engine on a draft listing; saves nothing", "S14b", has_body=True, action=A.LISTING_EDIT),
    # ---- jobs (S13) ----
    Operation("jobs_get", "GET", "/jobs/{job_id}", 200, JobEnvelope, "jobs",
              "The job envelope, read from job_runs", "S13", path_id="job_id", action=A.TENANT_READ),
    Operation("jobs_cancel", "POST", "/jobs/{job_id}/cancel", 200, JobEnvelope, "jobs",
              "Cancel a queued job", "S13", path_id="job_id", idempotency=True,
              conflict_code=E.INVALID_STATUS_TRANSITION, action=A.LISTING_GENERATE),
    # ---- issues and tasks (S19) ----
    Operation("issues_list", "GET", "/issues", 200, IssuePage, "issues",
              "The issue inbox (open issues by default)", "S19",
              query=("status", "listing_id", "source", "error_code", "marketplace", "limit",
                     "cursor"), action=A.TENANT_READ),
    Operation("issues_dismiss", "POST", "/issues/{issue_id}/dismiss", 200, IssueOut, "issues",
              "Dismiss an open warning (errors cannot be dismissed)", "S19", path_id="issue_id",
              idempotency=True, conflict_code=E.INVALID_STATUS_TRANSITION,
              action=A.LISTING_APPROVE),
    Operation("tasks_list", "GET", "/tasks", 200, TaskPage, "tasks",
              "The human task inbox (open tasks by default)", "S19",
              query=("status", "limit", "cursor"), action=A.TENANT_READ),
    # ---- analytics (S23) ----
    Operation("analytics_get", "GET", "/analytics", 200, AnalyticsSummary, "analytics",
              "Operational metrics (alias of /analytics/summary)", "S23", query=("from", "to"), action=A.ANALYTICS_READ),
    Operation("analytics_summary", "GET", "/analytics/summary", 200, AnalyticsSummary,
              "analytics", "Operational metrics for a date range", "S23", query=("from", "to"), action=A.ANALYTICS_READ),
)  # fmt: skip

OPS: Final[Mapping[str, Operation]] = MappingProxyType({op.op_id: op for op in _ROWS})
