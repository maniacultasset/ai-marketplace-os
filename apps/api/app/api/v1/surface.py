"""The frozen `/api/v1` surface: one row per operation (S3; BACKEND-ARCHITECTURE.md §5.2).

`OPS` is the frozen list (35 operations at S3, 38 with S10's switch and members, 40 with S15's
connect confirm and account revoke, 43 with S16's profile update and document list and delete,
44 with S20's listing preview, 48 with S19's listing history, issues, issue dismiss and tasks,
50 with S21's listing submissions and refresh-status, 55 with ADR-0019's sign-up, sign-up status
and the platform admin's list, approve and reject, 65 with DPP-1's MFA, step-up and password
routes (ADR-0021), 72 with Google sign-in's config, start, callback, complete, link, unlink and
sign-up (ADR-0020), 75 with the trusted browsers' list, remove and sign-out-all (ADR-0023), 80
with SI1's and SI3's product and task routes, 91 with SI5's invites, reset links and the platform
admin's tenant list, activate and extend-trial (ADR-0029), 112 with ADR-0032's brand create; no
dead-letter path, which is staff tooling per T82). It
drives the routers (`route()`), the declared OpenAPI responses (`declared_responses()`), the
parametrized surface tests and the case-matrix guard (`cases()`), so a route, its contract and its
required tests cannot drift apart.

Each operation names the one action it requires (`action`, CODE-PLAN S10 Q7; None for a public
op), and `roles` is derived from it through the role x action matrix (`app/core/rbac.py`), so the
registry, the runtime `require()` check and the declared 403s cannot drift apart. `cases()` and
`declared_responses()` read only `op.roles`. Every operation carries `x-mkos-action` in OpenAPI.

DPP-1 (ADR-0021, amended by ADR-0023): `step_up` marks the high-risk operations that need a code
entered in the last 30 minutes under `MFA_ENFORCEMENT=all` (marketplace connect and revoke, team
changes, recovery codes, Google unlink; replacing the authenticator checks it in the service) (`any`: a TOTP or a recovery code; `otp`: a TOTP code
only). `route()` adds `require_recent_mfa` after the op's policy, and the op declares the two 403
codes, `MFA_STEP_UP_REQUIRED` and `MFA_ENROLLMENT_REQUIRED`.
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
from app.api.v1.deps import public, require, require_platform_admin, require_recent_mfa
from app.api.v1.examples import (
    CALLBACK_LOCATION,
    ETAG_EXAMPLE,
    GOOGLE_AUTHORIZE_EXAMPLE,
    GOOGLE_CALLBACK_LOCATION,
    SUCCESS_EXAMPLES,
)
from app.api.v1.schemas.ai_credit import AiCreditOut
from app.api.v1.schemas.analytics import AnalyticsSummary, QualitySummary
from app.api.v1.schemas.auth import (
    LoginConfirmOut,
    LoginResult,
    MemberOut,
    MemberPage,
    MeOut,
    RecoveryCodesOut,
    SecurityOut,
    StepUpOut,
    TokenResponse,
    TotpConfirmOut,
    TotpEnrollOut,
    TrustedDevicesOut,
)
from app.api.v1.schemas.batches import BatchAccepted, BatchResult
from app.api.v1.schemas.brand import BrandDocumentPage
from app.api.v1.schemas.brand_profile import BrandProfileOut
from app.api.v1.schemas.catalog import (
    CategoryCatalogOut,
    CategoryDraftSuggestionOut,
    RequirementsOut,
)
from app.api.v1.schemas.category_suggestion import CategorySuggestionAccepted
from app.api.v1.schemas.column_maps import ColumnMapOut
from app.api.v1.schemas.google import GoogleCompleteOut, GoogleConfigOut
from app.api.v1.schemas.home import HomeSummaryOut
from app.api.v1.schemas.image_candidates import (
    ImageCandidateAccepted,
    ImageCandidateOut,
    ImageCandidatePage,
)
from app.api.v1.schemas.images import (
    ImageAccepted,
    ImageReportOut,
    PhotoMatchKeptOut,
    UploadUrlOut,
)
from app.api.v1.schemas.imports import ImportAccepted, ImportRowPage
from app.api.v1.schemas.invites import (
    InviteAccepted,
    InviteCreated,
    InviteJoined,
    InviteOut,
    InvitePage,
    InvitePreview,
    ResetLinkOut,
)
from app.api.v1.schemas.issue_history import IssueHistoryOut
from app.api.v1.schemas.issues import IssueOut, IssuePage
from app.api.v1.schemas.listings import (
    ListingDetail,
    ListingHistoryPage,
    ListingPage,
    ListingStatusOut,
    ListingVersionPage,
)
from app.api.v1.schemas.marketplaces import (
    ConnectOut,
    MarketplaceAccountOut,
    MarketplaceAccountPage,
)
from app.api.v1.schemas.product_delete import ProductBulkDeleteResult
from app.api.v1.schemas.products import ProductOut, ProductPage
from app.api.v1.schemas.resolution import ResolutionOut
from app.api.v1.schemas.rules import RulePage
from app.api.v1.schemas.signup import (
    SignupAccepted,
    SignupPollOut,
    SignupRequestOut,
    SignupRequestPage,
    SignupStatusOut,
)
from app.api.v1.schemas.size_chart_read import SizeChartReadOut
from app.api.v1.schemas.size_run import SizeRunOut
from app.api.v1.schemas.skus import SkuAvailabilityOut, SkuBatchCheckOut, SkuSuggestionOut
from app.api.v1.schemas.submissions import SubmissionPage
from app.api.v1.schemas.tasks import TaskOut, TaskPage
from app.api.v1.schemas.tenant_create import TenantCreated
from app.api.v1.schemas.tenants import TenantAdminOut, TenantAdminPage, TenantOut
from app.core.rbac import PLATFORM_ADMIN, PUBLIC, Action, roles_for

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
    """The action `require()` checks (CODE-PLAN S10 Q7); None exactly when the op is public or
    platform-admin only."""
    platform_admin: bool = False
    """Platform admins only (ADR-0019): `require_platform_admin()`, no role in the matrix."""
    roles: frozenset[Role] | None = None
    """Derived from `action` through the matrix when not given; a public op holds ALL_ROLES."""
    extra_codes: Mapping[int, tuple[ErrorCode, ...]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    """Codes beyond the D2 default for a status (login's 401, products_create's UNKNOWN_FACT_KEY),
    or a status of the op's own (listings_preview's 501 for a marketplace with no adapter)."""
    step_up: Literal["any", "otp"] | None = None
    """DPP-1: a code entered in the last 1800 s is required under `MFA_ENFORCEMENT=all`."""
    also_status: int | None = None
    """SI12: a second 2xx the op may answer with the same body (the size-chart read's 200 for a
    result already read, beside its 202 for a new job)."""

    def __post_init__(self) -> None:
        if self.public and self.platform_admin:
            raise ValueError(f"{self.op_id}: an op is public or platform-admin, not both")
        if (self.action is None) is not (self.public or self.platform_admin):
            raise ValueError(
                f"{self.op_id}: a public or platform-admin op has no action; any other op needs one"
            )
        if self.roles is None:
            if self.platform_admin:
                derived = NO_ROLES
            else:
                derived = ALL_ROLES if self.action is None else roles_for(self.action)
            object.__setattr__(self, "roles", derived)

    @property
    def policy(self) -> str:
        """The `x-mkos-action` value: the action, `public` or `platform:admin`."""
        if self.platform_admin:
            return PLATFORM_ADMIN
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
    # The body cap runs before routing on every request (BodyLimitMiddleware): 400 for a malformed
    # Content-Length (or a body FastAPI cannot parse), 413 over the cap, with or without a body.
    out: dict[int, tuple[ErrorCode, ...]] = {
        400: (E.VALIDATION_FAILED,),
        413: (E.PAYLOAD_TOO_LARGE,),
    }
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
    out[429] = (E.RATE_LIMITED,)
    if op.step_up is not None:
        out[403] = (*out.get(403, ()), *STEP_UP_CODES)
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


STEP_UP_CODES: Final = (E.MFA_STEP_UP_REQUIRED, E.MFA_ENROLLMENT_REQUIRED)

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
            "Any other failure, in the same envelope: 405 `METHOD_NOT_ALLOWED`, "
            "500 `INTERNAL_ERROR`."
        ),
        "content": {
            "application/json": {"example": error_example(500, E.INTERNAL_ERROR)},
        },
    }
    return out


# The redirect ops' success entry: (description, Location description, Location example).
REDIRECTS: Final[Mapping[str, tuple[str, str, str]]] = MappingProxyType(
    {
        "marketplaces_callback": (
            "Redirect to the app. `Location` carries only an opaque single-use `pending` handle: "
            "no code, token or account data.",
            "The fixed app route plus `?pending=<opaque>`.",
            CALLBACK_LOCATION,
        ),
        "auth_google_start": (
            "Redirect to Google's consent page (authorization code with PKCE S256, `state`, "
            "`nonce`, `prompt=select_account`, scopes `openid email profile`). With Google "
            "sign-in switched off it redirects to `/login?error=google_unavailable` instead.",
            "Google's authorize URL (or `/login?error=google_unavailable`).",
            GOOGLE_AUTHORIZE_EXAMPLE,
        ),
        "auth_google_callback": (
            "Redirect to the app's `/auth/google` route with an opaque single-use `handle` "
            "(60 seconds, bound to the browser), or `?error=cancelled` (the user backed out at "
            "Google), `?error=failed` or `?error=unavailable`: no code, token or account data.",
            "`{dashboard}/auth/google?handle=<opaque>` or `?error=cancelled|failed|unavailable`.",
            GOOGLE_CALLBACK_LOCATION,
        ),
    }
)


def success_response(op: Operation) -> dict[int | str, dict[str, Any]]:
    """The 2xx/3xx entry: the op's success example, or the redirect `Location` / 204 text."""
    if op.op_id in REDIRECTS:
        description, location, example = REDIRECTS[op.op_id]
        return {
            op.status: {
                "description": description,
                "headers": {
                    "Location": {
                        "description": location,
                        "schema": {"type": "string"},
                        "example": example,
                    }
                },
            }
        }
    if op.response_model is None:
        return {op.status: {"description": "Done; no body."}}
    entry: dict[str, Any] = {
        "content": {"application/json": {"example": SUCCESS_EXAMPLES[op.op_id]}}
    }
    if op.also_status is not None:
        also = {
            "model": op.response_model,
            "description": "Done; answered from a result already stored (no new job).",
            "content": {"application/json": {"example": SUCCESS_EXAMPLES[op.op_id]}},
        }
        return {op.status: entry, op.also_status: also}
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
    if op.platform_admin:
        return require_platform_admin()
    if op.action is None:
        return public()
    return require(op.action)


def _dependencies(op: Operation) -> list[Any]:
    """The op's policy, then (DPP-1) the step-up guard of a sensitive op."""
    deps = [_policy(op)]
    if op.step_up is not None:
        deps.append(require_recent_mfa(otp_only=op.step_up == "otp"))
    return deps


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
            dependencies=_dependencies(op),
            openapi_extra={"x-mkos-action": op.policy},
            **kwargs,
        )
        return fn

    return decorate


A = Action
_401: Final = MappingProxyType({401: (E.UNAUTHENTICATED,)})
# SI10: a daily cap (429) or the platform AI pause (503) refuses a generate or regenerate.
_AI_ADMISSION: Final = MappingProxyType(
    {429: (E.BUDGET_EXCEEDED,), 503: (E.AI_CREDIT_EXHAUSTED, E.AI_PROVIDER_KEY_INVALID)}
)
# SI10 D4: an AI image edit while AI is paused (a daily cap is a `failed/budget` candidate)
_AI_PAUSED: Final = MappingProxyType({503: (E.AI_CREDIT_EXHAUSTED, E.AI_PROVIDER_KEY_INVALID)})
# SI12: the size-chart read: a variant (FACT_LEVEL_INVALID), and the SI10 admission
_SIZE_CHART_READ: Final = MappingProxyType(
    {
        422: (E.FACT_LEVEL_INVALID,),
        429: (E.BUDGET_EXCEEDED,),
        503: (E.AI_CREDIT_EXHAUSTED, E.AI_PROVIDER_KEY_INVALID),
    }
)
# S22 D2: a marketplace that is not configured or is switched off (FLIPKART_ENABLED=false).
_501: Final = MappingProxyType({501: (E.NOT_IMPLEMENTED,)})
# DPP-1: the public challenge routes (a wrong code or burned challenge is 401; 501 under `off`)
_MFA_PUBLIC: Final = MappingProxyType({401: (E.UNAUTHENTICATED,), 501: (E.NOT_IMPLEMENTED,)})
# DPP-1: the bearer enrolment pair (403 when replacing without a fresh step-up; 501 under `off`)
_MFA_BEARER: Final = MappingProxyType({403: (E.MFA_STEP_UP_REQUIRED,), 501: (E.NOT_IMPLEMENTED,)})
# ADR-0020: the Google JSON steps (an unknown, used, expired or foreign handle is 401
# OAUTH_STATE_INVALID; the generic failure is 401 UNAUTHENTICATED; 501 with the flag off)
_GOOGLE_PUBLIC: Final = MappingProxyType(
    {401: (E.UNAUTHENTICATED, E.OAUTH_STATE_INVALID), 501: (E.NOT_IMPLEMENTED,)}
)
# SI10: a verified sign-in whose only brands are suspended is told so (403)
_401_SUSPENDED: Final = MappingProxyType({**_401, 403: (E.TENANT_SUSPENDED,)})
_GOOGLE_COMPLETE: Final = MappingProxyType({**_GOOGLE_PUBLIC, 403: (E.TENANT_SUSPENDED,)})

# SI5: the generic refusal of a copied link, and a bad Google handle on accept (OAUTH_STATE_INVALID)
# or Google sign-in switched off (501).
_404: Final = MappingProxyType({404: (E.NOT_FOUND,)})
_INVITE_ACCEPT: Final = MappingProxyType({**_GOOGLE_PUBLIC, **_404})

# PD1: a product delete refused while a listing is generating or being sent
_DELETE_BUSY: Final = MappingProxyType({409: (E.LISTING_BUSY, E.SUBMISSION_IN_FLIGHT)})

_ROWS: Final[tuple[Operation, ...]] = (
    # ---- auth (S10) ----
    Operation("auth_login", "POST", "/auth/login", 200, LoginResult, "auth",
              "Log in with email and password: tokens, or the next sign-in step", "S10",
              public=True, has_body=True, extra_codes=_401_SUSPENDED),
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
    # ---- MFA, step-up and passwords (DPP-1, ADR-0021) ----
    Operation("auth_login_mfa", "POST", "/auth/login/mfa", 200, LoginResult, "auth",
              "Sign-in code step: a TOTP code or a recovery code for the login challenge",
              "DPP-1", public=True, has_body=True, extra_codes=_MFA_PUBLIC),
    Operation("auth_login_mfa_enroll", "POST", "/auth/login/mfa/enroll", 200, TotpEnrollOut,
              "auth", "Forced enrolment at sign-in: a new authenticator secret", "DPP-1",
              public=True, has_body=True, extra_codes=_MFA_PUBLIC),
    Operation("auth_login_mfa_confirm", "POST", "/auth/login/mfa/confirm", 200,
              LoginConfirmOut, "auth",
              "Forced enrolment at sign-in: confirm with a code; recovery codes and the sign-in "
              "result", "DPP-1", public=True, has_body=True, extra_codes=_MFA_PUBLIC),
    Operation("auth_password_expired", "POST", "/auth/password/expired", 200, LoginResult,
              "auth", "Forced password change at sign-in", "DPP-1", public=True, has_body=True,
              extra_codes=_MFA_PUBLIC),
    Operation("auth_mfa_totp_enroll", "POST", "/auth/mfa/totp/enroll", 200, TotpEnrollOut,
              "auth", "Set up or replace the authenticator from account settings", "DPP-1",
              has_body=True, action=A.TENANT_READ, extra_codes=_MFA_BEARER),
    Operation("auth_mfa_totp_confirm", "POST", "/auth/mfa/totp/confirm", 200, TotpConfirmOut,
              "auth", "Confirm the new authenticator with a code; recovery codes", "DPP-1",
              has_body=True, action=A.TENANT_READ, extra_codes=_MFA_BEARER),
    Operation("auth_security", "GET", "/auth/security", 200, SecurityOut, "auth",
              "Account security: MFA status, recovery codes left, password expiry", "DPP-1",
              action=A.TENANT_READ),
    Operation("auth_mfa_recovery_codes", "POST", "/auth/mfa/recovery-codes", 200,
              RecoveryCodesOut, "auth", "Regenerate the recovery codes (after a step-up)",
              "DPP-1", action=A.TENANT_READ, step_up="any", extra_codes=_501),
    Operation("auth_mfa_step_up", "POST", "/auth/mfa/step-up", 200, StepUpOut, "auth",
              "Step up with a code: a new access token for sensitive actions", "DPP-1",
              has_body=True, action=A.TENANT_READ,
              extra_codes=MappingProxyType({**_401, **_501})),
    Operation("auth_password_change", "POST", "/auth/password", 204, None, "auth",
              "Change the password (current password, DPP rules, not one of the last 10)",
              "DPP-1", has_body=True, action=A.TENANT_READ),
    Operation("auth_password_reset", "POST", "/auth/password/reset", 204, None, "auth",
              "Set a new password with a copied reset link (DPP rules, not one of the last 10)",
              "SI5", public=True, has_body=True, extra_codes=_404),
    # ---- Google sign-in, always followed by the Kuinn code (ADR-0020) ----
    Operation("auth_google_config", "GET", "/auth/google/config", 200, GoogleConfigOut,
              "auth", "Whether sign-in with Google is switched on (show the button)",
              "ADR-0020", public=True),
    Operation("auth_google_start", "GET", "/auth/google/start", 302, None, "auth",
              "Sign in or sign up with Google: redirect to Google's consent page",
              "ADR-0020", public=True, query=("b",)),
    Operation("auth_google_callback", "GET", "/auth/google/callback", 303, None, "auth",
              "Google's return: 303 to the app with an opaque single-use handle", "ADR-0020",
              public=True, query=("code", "state", "error")),
    Operation("auth_google_complete", "POST", "/auth/google/complete", 200, GoogleCompleteOut,
              "auth", "Finish the Google step: sign in (or the code step), pending, the "
              "prefilled sign-up, or the password step that links Google", "ADR-0020",
              public=True, has_body=True, extra_codes=_GOOGLE_COMPLETE),
    Operation("auth_google_link", "POST", "/auth/google/link", 200, LoginResult, "auth",
              "Link Google to an existing account: its password, then the usual code steps",
              "ADR-0020", public=True, has_body=True, extra_codes=_GOOGLE_PUBLIC),
    Operation("auth_google_unlink", "POST", "/auth/google/unlink", 204, None, "auth",
              "Remove the linked Google account (after a step-up; needs a password)",
              "ADR-0020", action=A.TENANT_READ, step_up="any",
              extra_codes=MappingProxyType({422: (E.VALIDATION_FAILED,)})),
    # ---- trusted browsers (ADR-0023): the caller's own, no step-up ----
    Operation("auth_devices_list", "GET", "/auth/devices", 200, TrustedDevicesOut, "auth",
              "The caller's trusted browsers (the code is skipped there at sign-in)",
              "ADR-0023", action=A.TENANT_READ),
    Operation("auth_devices_remove", "DELETE", "/auth/devices/{device_id}", 204, None, "auth",
              "Forget one trusted browser and end the sessions it began", "ADR-0023",
              action=A.TENANT_READ, extra_codes=MappingProxyType({404: (E.NOT_FOUND,)})),
    Operation("auth_devices_sign_out_all", "POST", "/auth/devices/sign-out-all", 204, None,
              "auth", "Forget every trusted browser and end every other session", "ADR-0023",
              action=A.TENANT_READ),
    # ---- members (S10) ----
    Operation("members_list", "GET", "/members", 200, MemberPage, "members",
              "List the tenant's members", "S10", query=("limit", "cursor"),
              action=A.TEAM_MANAGE),
    Operation("members_patch", "PATCH", "/members/{user_id}", 200, MemberOut, "members",
              "Change a member's role or active flag (only an owner grants or removes owner)",
              "S10", path_id="user_id", has_body=True, conflict_code=E.LAST_OWNER_REQUIRED,
              action=A.TEAM_MANAGE, step_up="any"),
    Operation("members_password_reset", "POST", "/members/{user_id}/password-reset", 201,
              ResetLinkOut, "members",
              "A copied password reset link for a member (24 hours, once; sole-tenant members "
              "with an authenticator under MFA_ENFORCEMENT=all)", "SI5", path_id="user_id",
              action=A.TEAM_MANAGE, step_up="otp"),
    # ---- team invites by copied link (SI5, ADR-0029) ----
    Operation("invites_create", "POST", "/invites", 201, InviteCreated, "invites",
              "Invite a teammate: a copied link for 7 days (a new invite for the email replaces "
              "the old one)", "SI5", has_body=True, action=A.TEAM_MANAGE, step_up="any"),
    Operation("invites_list", "GET", "/invites", 200, InvitePage, "invites",
              "List the tenant's invites, newest first", "SI5",
              query=("status", "limit", "cursor"), action=A.TEAM_MANAGE),
    Operation("invites_revoke", "POST", "/invites/{invite_id}/revoke", 200, InviteOut,
              "invites", "Revoke an invite (idempotent; an accepted invite is 409)", "SI5",
              path_id="invite_id", conflict_code=E.INVALID_STATUS_TRANSITION,
              action=A.TEAM_MANAGE),
    Operation("invites_lookup", "POST", "/invites/lookup", 200, InvitePreview, "invites",
              "What an invite link is for: the brand, the role and whether the email has an "
              "account", "SI5", public=True, has_body=True, extra_codes=_404),
    Operation("invites_accept", "POST", "/invites/accept", 200, InviteAccepted, "invites",
              "Accept an invite with a new account (a password, or the matching Google "
              "identity)", "SI5", public=True, has_body=True,
              conflict_code=E.INVALID_STATUS_TRANSITION, extra_codes=_INVITE_ACCEPT),
    Operation("invites_join", "POST", "/invites/join", 200, InviteJoined, "invites",
              "Join the inviting brand with the signed-in account whose email matches", "SI5",
              has_body=True, conflict_code=E.INVALID_STATUS_TRANSITION, extra_codes=_404,
              action=A.TENANT_READ),
    # ---- tenants and brand (S16) ----
    Operation("tenants_me", "GET", "/tenants/me", 200, TenantOut, "tenants",
              "The brand profile", "S16", etag=True, action=A.TENANT_READ),
    # ADR-0031: `owner_self_approval` is owner only and needs a step-up (checked in the route,
    # for that field only, so a profile edit never asks for a code)
    Operation("tenants_me_update", "PATCH", "/tenants/me", 200, TenantOut, "tenants",
              "Update the brand profile (If-Match: the ETag from GET)", "S16", has_body=True,
              if_match=True, conflict_code=E.STALE_VERSION, etag=True,
              action=A.SETTINGS_MANAGE, extra_codes={403: STEP_UP_CODES}),
    # ADR-0032: an owner adds another brand without a new sign-up review (403 for an admin, a
    # brand owner with a suspended brand, or the owned-brand cap; 501 while the switch is off)
    Operation("tenants_create", "POST", "/tenants", 201, TenantCreated, "tenants",
              "Add another brand: a new trial brand the caller owns (no sign-up review)",
              "ADR-0032", has_body=True, idempotency=True, action=A.SETTINGS_MANAGE,
              step_up="any", extra_codes=_501),
    Operation("brand_documents_upload", "POST", "/brand/documents", 202, JobAccepted, "brand",
              "Upload a brand document for chunking", "S16", has_body=True, idempotency=True, action=A.SETTINGS_MANAGE),
    Operation("brand_documents_list", "GET", "/brand/documents", 200, BrandDocumentPage,
              "brand", "List the brand documents", "S16", query=("limit", "cursor"),
              action=A.TENANT_READ),
    Operation("brand_documents_delete", "DELETE", "/brand/documents/{document_id}", 204, None,
              "brand", "Delete a brand document and deactivate its chunks", "S16",
              path_id="document_id", action=A.SETTINGS_MANAGE),
    # ---- brand (SI1) ----
    Operation("brand_profile_get", "GET", "/brand-profile", 200, BrandProfileOut, "brand",
              "The brand profile: the default manufacturer, website, description and voice", "SI1",
              etag=True, action=A.TENANT_READ),
    Operation("brand_profile_update", "PATCH", "/brand-profile", 200, BrandProfileOut, "brand",
              "Update the brand profile (If-Match: the ETag from GET)", "SI1", has_body=True,
              if_match=True, conflict_code=E.STALE_VERSION, etag=True,
              action=A.SETTINGS_MANAGE),
    # ---- products (S12, S13) ----
    Operation("products_create", "POST", "/products", 201, ProductOut, "products",
              "Create one product", "S12", has_body=True,
              body_ids=("images", "image_uploads[].storage_key"),
              conflict_code=E.SKU_CONFLICT, etag=True,
              extra_codes=MappingProxyType({422: (E.UNKNOWN_FACT_KEY,)}), action=A.PRODUCT_IMPORT),
    Operation("products_list", "GET", "/products", 200, ProductPage, "products",
              "List products", "S12", query=("limit", "cursor"), action=A.TENANT_READ),
    Operation("products_get", "GET", "/products/{product_id}", 200, ProductOut, "products",
              "Read one product", "S12", path_id="product_id", etag=True,
              action=A.TENANT_READ),
    Operation("products_update", "PATCH", "/products/{product_id}", 200, ProductOut, "products",
              "Edit a product (If-Match: the ETag from GET)", "SI1", path_id="product_id",
              has_body=True, if_match=True, conflict_code=E.STALE_VERSION, etag=True,
              extra_codes=MappingProxyType({422: (E.FACT_LEVEL_INVALID, E.UNKNOWN_FACT_KEY)}),
              action=A.PRODUCT_IMPORT),
    Operation("products_recategorize", "POST", "/products/{product_id}/recategorize", 200,
              ProductOut, "products",
              "Change the category of a product that has listings; its unpublished listings "
              "take the new category and wait for a regenerate (409 when any listing was "
              "published)", "SI9", path_id="product_id", has_body=True,
              conflict_code=E.INVALID_STATUS_TRANSITION, etag=True,
              extra_codes=MappingProxyType({409: (E.LISTING_BUSY,)}), action=A.PRODUCT_IMPORT),
    Operation("products_bulk", "POST", "/products/bulk", 202, ImportAccepted, "products",
              "Import products from a CSV file", "S13", has_body=True, idempotency=True,
              extra_codes=MappingProxyType({422: (E.IMPORT_FAILED,)}), action=A.PRODUCT_IMPORT),
    # ---- product delete (PD1) ----
    Operation("products_delete", "DELETE", "/products/{product_id}", 204, None, "products",
              "Delete a product and its sizes: open questions close, queued work stops, its "
              "listings are archived (409 when a listing was sent to a marketplace)", "PD1",
              path_id="product_id", conflict_code=E.INVALID_STATUS_TRANSITION,
              extra_codes=_DELETE_BUSY, action=A.SETTINGS_MANAGE),
    Operation("products_bulk_delete", "POST", "/products/bulk-delete", 200,
              ProductBulkDeleteResult, "products",
              "Delete up to 100 products, each answered on its own", "PD1", has_body=True,
              body_ids=("product_ids[]",), idempotency=True, action=A.SETTINGS_MANAGE),
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
              idempotency=True, action=A.LISTING_GENERATE, extra_codes=_AI_ADMISSION),
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
    # item 17 (client feedback, 11 Nov 2026): who made, edited and approved each version
    Operation("listings_versions", "GET", "/listings/{listing_id}/versions", 200,
              ListingVersionPage, "listings", "The listing's versions with who made, edited "
              "and approved each, newest first", "S19", path_id="listing_id",
              query=("limit", "cursor"), action=A.TENANT_READ),
    Operation("listings_content_patch", "PATCH", "/listings/{listing_id}/content", 200,
              ListingDetail, "listings", "Edit content (creates a new version)", "S18",
              path_id="listing_id", has_body=True, if_match=True,
              conflict_code=E.STALE_VERSION, etag=True, action=A.LISTING_EDIT),
    Operation("listings_regenerate", "POST", "/listings/{listing_id}/regenerate", 202,
              JobAccepted, "listings", "Regenerate an existing listing", "S18",
              path_id="listing_id", has_body=True, idempotency=True, action=A.LISTING_REGENERATE,
              extra_codes=_AI_ADMISSION),
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
    Operation("listings_resolution", "GET", "/listings/{listing_id}/resolution", 200,
              ResolutionOut, "listings", "What Kuinn tried, changed and needs for this listing",
              "SI11", path_id="listing_id", action=A.TENANT_READ),
    Operation("listings_issue_history", "GET", "/listings/{listing_id}/issue-history", 200,
              IssueHistoryOut, "listings", "Every marketplace issue of this listing, who handles "
              "it, and each fix Kuinn made (before, after, reason, outcome)", "SI11",
              path_id="listing_id", action=A.TENANT_READ),
    Operation("listings_refresh_status", "POST", "/listings/{listing_id}/refresh-status", 202,
              JobAccepted, "listings", "Check the marketplace status now (at most once per 5 "
              "minutes per listing)", "S21", path_id="listing_id", idempotency=True,
              conflict_code=E.INVALID_STATUS_TRANSITION,
              extra_codes=MappingProxyType({409: (E.LISTING_BUSY,)}), action=A.LISTING_PUBLISH),
    # ---- marketplaces (S15) ----
    Operation("marketplaces_connect", "POST", "/marketplaces/connect", 200, ConnectOut,
              "marketplaces", "Start OAuth; returns the consent URL", "S15", has_body=True,
              extra_codes=_501, action=A.MARKETPLACE_CONNECT, step_up="any"),
    Operation("marketplaces_callback", "GET", "/marketplaces/{marketplace}/callback", 303, None,
              "marketplaces", "OAuth return: 303 to the app with an opaque pending handle",
              "S15", public=True,
              query=("code", "state", "spapi_oauth_code", "selling_partner_id"), oauth_state=True,
              extra_codes=MappingProxyType({
                  **_501,
                  # the token exchange: a malformed answer (502), a 5xx or timeout (503)
                  502: (E.MARKETPLACE_UNKNOWN_ERROR,),
                  503: (E.MARKETPLACE_UNAVAILABLE,),
              })),
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
              action=A.MARKETPLACE_CONNECT, step_up="otp"),
    # ---- images (S17b) ----
    Operation("images_upload_url", "POST", "/images/upload-url", 200, UploadUrlOut, "images",
              "A signed upload URL for one image", "S17b", has_body=True,
              body_ids=("product_id",), action=A.PRODUCT_IMPORT),
    Operation("images_upload", "POST", "/images/upload", 202, ImageAccepted, "images",
              "Upload an image (or name an uploaded key) and start the checks", "S17b",
              has_body=True, body_ids=("product_id", "storage_key"), idempotency=True, action=A.PRODUCT_IMPORT),
    Operation("images_report", "GET", "/images/{image_id}/report", 200, ImageReportOut, "images",
              "The image compliance reports", "S17b", path_id="image_id", action=A.TENANT_READ),
    Operation("images_delete", "DELETE", "/images/{image_id}", 204, None, "images",
              "Delete an image (and its prepared and public copies)", "SI1",
              path_id="image_id", action=A.PRODUCT_IMPORT),
    # ---- image candidates (SI7) ----
    Operation("image_candidates_create", "POST", "/products/{product_id}/image-candidates", 202,
              ImageCandidateAccepted, "images",
              "Make an image candidate from a photo or the size chart", "SI7",
              path_id="product_id", has_body=True, body_ids=("source_image_id",),
              idempotency=True, conflict_code=E.INVALID_STATUS_TRANSITION,
              action=A.PRODUCT_IMPORT, extra_codes=_AI_PAUSED),
    Operation("image_candidates_list", "GET", "/products/{product_id}/image-candidates", 200,
              ImageCandidatePage, "images", "The product's image candidates", "SI7",
              path_id="product_id", action=A.TENANT_READ),
    Operation("image_candidates_accept", "POST", "/image-candidates/{candidate_id}/accept", 202,
              ImageAccepted, "images", "Use a candidate as a product image", "SI7",
              path_id="candidate_id", idempotency=True,
              conflict_code=E.INVALID_STATUS_TRANSITION, action=A.PRODUCT_IMPORT),
    Operation("image_candidates_reject", "POST", "/image-candidates/{candidate_id}/reject", 200,
              ImageCandidateOut, "images", "Discard a candidate", "SI7",
              path_id="candidate_id", idempotency=True,
              conflict_code=E.INVALID_STATUS_TRANSITION, action=A.PRODUCT_IMPORT),
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
    Operation("tasks_answer", "POST", "/tasks/{task_id}/answer", 200, TaskOut, "tasks",
              "Answer a fact question", "SI3", path_id="task_id", has_body=True,
              idempotency=True, conflict_code=E.INVALID_STATUS_TRANSITION,
              extra_codes=MappingProxyType({422: (E.FACT_LEVEL_INVALID, E.UNKNOWN_FACT_KEY)}),
              action=A.PRODUCT_IMPORT),
    Operation("tasks_close", "POST", "/tasks/{task_id}/close", 200, TaskOut, "tasks",
              "Close an account task (marketplace_error_review, permission_decision) as done or "
              "dismissed", "SI9", path_id="task_id", has_body=True, idempotency=True,
              conflict_code=E.INVALID_STATUS_TRANSITION, action=A.MARKETPLACE_CONNECT),
    # ---- analytics (S23) ----
    Operation("analytics_get", "GET", "/analytics", 200, AnalyticsSummary, "analytics",
              "Operational metrics (alias of /analytics/summary)", "S23", query=("from", "to"), action=A.ANALYTICS_READ),
    Operation("analytics_summary", "GET", "/analytics/summary", 200, AnalyticsSummary,
              "analytics", "Operational metrics for a date range", "S23", query=("from", "to"), action=A.ANALYTICS_READ),
    # ---- self-serve sign-up and the platform admin's queue (ADR-0019) ----
    Operation("signup_create", "POST", "/signup", 202, SignupAccepted, "signup",
              "Sign up a brand: a pending request a platform admin approves", "ADR-0019",
              public=True, has_body=True),
    Operation("signup_status", "POST", "/signup/status", 200, SignupStatusOut, "signup",
              "The caller's latest sign-up request (email and password)", "ADR-0019",
              public=True, has_body=True, extra_codes=_401),
    Operation("signup_status_poll", "POST", "/signup/status/poll", 200, SignupPollOut, "signup",
              "A sign-up request's status by its status handle (no password; read only)",
              "ADR-0019", public=True, has_body=True, extra_codes=_404),
    Operation("signup_status_reply", "POST", "/signup/status/reply", 200, SignupPollOut, "signup",
              "Reply once to the admin's question, by the status handle (no password)",
              "ADR-0019", public=True, has_body=True, conflict_code=E.INVALID_STATUS_TRANSITION,
              extra_codes=_404),
    Operation("signup_google", "POST", "/signup/google", 202, SignupStatusOut, "signup",
              "Sign up with the Google identity: a pending request a platform admin approves",
              "ADR-0020", public=True, has_body=True, extra_codes=_GOOGLE_PUBLIC),
    Operation("admin_signup_requests_list", "GET", "/admin/signup-requests", 200,
              SignupRequestPage, "admin", "List sign-up requests, oldest first (platform admins)",
              "ADR-0019", query=("status", "limit", "cursor"), platform_admin=True),
    Operation("admin_signup_requests_approve", "POST",
              "/admin/signup-requests/{request_id}/approve", 200, SignupRequestOut, "admin",
              "Approve a sign-up: create the trial tenant and its owner (idempotent)", "ADR-0019",
              has_body=True, conflict_code=E.INVALID_STATUS_TRANSITION,
              extra_codes=MappingProxyType({404: (E.NOT_FOUND,)}), platform_admin=True),
    Operation("admin_signup_requests_reject", "POST",
              "/admin/signup-requests/{request_id}/reject", 200, SignupRequestOut, "admin",
              "Reject a sign-up with a reason (idempotent)", "ADR-0019", has_body=True,
              conflict_code=E.INVALID_STATUS_TRANSITION,
              extra_codes=MappingProxyType({404: (E.NOT_FOUND,)}), platform_admin=True),
    Operation("admin_signup_requests_ask", "POST",
              "/admin/signup-requests/{request_id}/ask", 200, SignupRequestOut, "admin",
              "Ask the applicant one question; the request stays pending (idempotent)",
              "ADR-0019", has_body=True, conflict_code=E.INVALID_STATUS_TRANSITION,
              extra_codes=MappingProxyType({404: (E.NOT_FOUND,)}), platform_admin=True),
    # ---- trial recovery (SI5, ADR-0029) ----
    Operation("admin_tenants_list", "GET", "/admin/tenants", 200, TenantAdminPage, "admin",
              "List every tenant with its status and trial end, newest first (platform admins)",
              "SI5", query=("status", "limit", "cursor"), platform_admin=True),
    Operation("admin_tenants_activate", "POST", "/admin/tenants/{target_tenant_id}/activate", 200,
              TenantAdminOut, "admin",
              "Activate a trial or suspended tenant, with a reason (idempotent)", "SI5",
              has_body=True, conflict_code=E.INVALID_STATUS_TRANSITION, extra_codes=_404,
              platform_admin=True),
    Operation("admin_tenants_extend_trial", "POST", "/admin/tenants/{target_tenant_id}/extend-trial",
              200, TenantAdminOut, "admin",
              "Extend a trial by 1-90 days, or reopen a trial that expired, with a reason", "SI5",
              has_body=True, conflict_code=E.INVALID_STATUS_TRANSITION, extra_codes=_404,
              platform_admin=True),
    # SI10: the AI credit card (platform admins)
    Operation("admin_ai_credit_get", "GET", "/admin/ai-credit", 200, AiCreditOut, "admin",
              "The AI credit: pause, last key check, floor and today's platform spend", "SI10",
              platform_admin=True),
    Operation("admin_ai_credit_resume", "POST", "/admin/ai-credit/resume", 200, AiCreditOut,
              "admin", "Resume AI after a top-up or a key fix (repeatable)", "SI10",
              platform_admin=True),
    # ---- SI12: the size-chart read ----
    Operation("size_chart_read_create", "POST", "/products/{product_id}/size-chart-read", 202,
              SizeChartReadOut, "products", "Read the size-chart photo into a suggestion", "SI12",
              path_id="product_id", idempotency=True,
              conflict_code=E.INVALID_STATUS_TRANSITION, action=A.PRODUCT_IMPORT,
              extra_codes=_SIZE_CHART_READ, also_status=200),
    # ---- SI12: what each marketplace needs ----
    Operation("catalog_requirements_get", "GET", "/catalog/requirements", 200, RequirementsOut,
              "catalog", "What each marketplace needs for a category", "SI12",
              query=("category",), action=A.TENANT_READ),
    # ---- client feedback 11 Nov: the category picker, the suggestion before save, the SKU ----
    Operation("catalog_categories_list", "GET", "/catalog/categories", 200, CategoryCatalogOut,
              "catalog", "The categories you can pick, and what each marketplace is sent",
              "SI14", action=A.TENANT_READ),
    Operation("catalog_category_suggest", "POST", "/catalog/category-suggestions", 202,
              CategoryDraftSuggestionOut, "catalog",
              "Suggest a category for a product before it is saved", "SI14", has_body=True,
              idempotency=True, action=A.PRODUCT_IMPORT, extra_codes=_AI_ADMISSION,
              also_status=200),
    Operation("catalog_category_suggestion_get", "GET",
              "/catalog/category-suggestions/{suggestion_id}", 200, CategoryDraftSuggestionOut,
              "catalog", "Read a category suggestion", "SI14", path_id="suggestion_id",
              action=A.TENANT_READ),
    Operation("skus_availability", "GET", "/skus/availability", 200, SkuAvailabilityOut,
              "products", "Whether a SKU is free in this brand", "SI14", query=("sku",),
              action=A.TENANT_READ),
    Operation("skus_availability_check", "POST", "/skus/availability-check", 200,
              SkuBatchCheckOut, "products",
              "Which of many SKUs are already used in this brand", "SI14", has_body=True,
              action=A.TENANT_READ),
    Operation("skus_suggest", "POST", "/skus/suggestion", 200, SkuSuggestionOut, "products",
              "Make a new SKU for this brand", "SI14", has_body=True, idempotency=True,
              action=A.PRODUCT_IMPORT),
    # ---- SI14: the V1.1 assist (category suggestion) ----
    Operation("products_category_suggest", "POST", "/products/{product_id}/category-suggestion",
              202, CategorySuggestionAccepted, "products",
              "Suggest a category for the seller to confirm", "SI14",
              path_id="product_id", idempotency=True, action=A.PRODUCT_IMPORT,
              extra_codes=_SIZE_CHART_READ, also_status=200),
    # ---- V1.1: the size run from the saved size chart ----
    Operation("products_size_run_create", "POST", "/products/{product_id}/size-run", 201,
              SizeRunOut, "products", "Make the size variants from the saved size chart",
              "SI14", path_id="product_id", has_body=True, idempotency=True,
              conflict_code=E.SKU_CONFLICT, action=A.PRODUCT_IMPORT, also_status=200,
              extra_codes=MappingProxyType({
                  409: (E.INVALID_STATUS_TRANSITION, E.LISTING_BUSY),
                  422: (E.FACT_LEVEL_INVALID, E.VARIATION_INVALID),
              })),
    # ---- SI14: the spreadsheet column maps ----
    Operation("imports_column_map_create", "POST", "/imports/column-maps", 202, ColumnMapOut,
              "imports", "Map a spreadsheet header row onto the import columns", "SI14",
              has_body=True, idempotency=True, action=A.PRODUCT_IMPORT, also_status=201),
    Operation("imports_column_map_get", "GET", "/imports/column-maps/{map_id}", 200, ColumnMapOut,
              "imports", "One column map", "SI14", path_id="map_id", action=A.TENANT_READ),
    Operation("imports_column_map_confirm", "POST", "/imports/column-maps/{map_id}/confirm", 200,
              ColumnMapOut, "imports", "Confirm a column map with your answers", "SI14",
              path_id="map_id", has_body=True, idempotency=True,
              conflict_code=E.INVALID_STATUS_TRANSITION, action=A.PRODUCT_IMPORT,
              extra_codes=MappingProxyType({422: (E.IMPORT_FAILED,)})),
    # ---- SI14: the quality dashboard ----
    # ---- V1.1 (client feedback item 9): the variant photo match ----
    Operation("images_photo_match_keep", "POST", "/images/{image_id}/photo-match/keep", 200,
              PhotoMatchKeptOut, "images", "Keep a photo the photo match questioned", "SI14",
              path_id="image_id", idempotency=True, conflict_code=E.INVALID_STATUS_TRANSITION,
              action=A.PRODUCT_IMPORT),
    Operation("analytics_quality", "GET", "/analytics/quality", 200, QualitySummary, "analytics",
              "First-pass, blocked and edit rates and cost per listing", "SI14",
              query=("from", "to"), action=A.ANALYTICS_READ),
    Operation("admin_quality", "GET", "/admin/quality", 200, QualitySummary, "admin",
              "The quality dashboard across tenants", "SI14",
              query=("from", "to", "target_tenant_id"), platform_admin=True),
    # ---- client feedback 11 Nov 2026, item 6: the Home summary ----
    Operation("home_summary", "GET", "/home/summary", 200, HomeSummaryOut, "home",
              "Exact listing counts, what needs you, recent agent work and marketplace status",
              "FB6", action=A.TENANT_READ),
)  # fmt: skip

OPS: Final[Mapping[str, Operation]] = MappingProxyType({op.op_id: op for op in _ROWS})
